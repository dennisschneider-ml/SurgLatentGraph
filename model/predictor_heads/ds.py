from mmdet.registry import MODELS
from abc import ABCMeta
from mmengine.model import BaseModule
from mmdet.utils import ConfigType, OptConfigType, InstanceList, OptMultiConfig
from mmengine.structures import BaseDataElement
from mmdet.structures import SampleList
from .modules.gnn import GNNHead
from .modules.layers import build_mlp, PositionalEncoding
import torch
from torch import Tensor
from torch_scatter import scatter_mean
import torch.nn.functional as F
from typing import List, Union
import torch.nn.functional as F

@MODELS.register_module()
class DSHead(BaseModule, metaclass=ABCMeta):
    """DS Head to predict downstream task from graph

    Args:
        num_classes (int)
        gnn_cfg (ConfigType): gnn cfg
        img_feat_key (str): use bb feats or fpn feats for img-level features
        graph_feat_input_dim (int): node and edge feat dim in graph structure
        graph_feat_projected_dim (int): node and edge feat dim to use in gnn
        loss (str): loss fn for ds (default: BCELoss)
        loss_consensus (str): how to deal with multiple annotations for ds task (default: mode)
        weight (List): per-class loss weight for ds (default: None)
        loss_weight: multiplier for ds loss
    """
    def __init__(self, num_classes: int, gnn_cfg: ConfigType,
            img_feat_key: str, img_feat_size: int, input_viz_feat_size: int,
            input_sem_feat_size: int, final_viz_feat_size: int, final_sem_feat_size: int,
            loss: Union[List, ConfigType], use_img_feats=True, loss_consensus: str = 'mode',
            num_predictor_layers: int = 2, loss_weight: float = 1.0,
            init_cfg: OptMultiConfig = None) -> None:
        super().__init__(init_cfg=init_cfg)

        # set viz and sem dims for projecting node/edge feats in input graph
        self.input_sem_feat_size = input_sem_feat_size
        self.input_viz_feat_size = input_viz_feat_size
        self.final_sem_feat_size = final_sem_feat_size
        self.final_viz_feat_size = final_viz_feat_size
        self.img_feat_size = img_feat_size

        self.node_viz_feat_projector = torch.nn.Linear(input_viz_feat_size, final_viz_feat_size)
        self.edge_viz_feat_projector = torch.nn.Linear(input_viz_feat_size, final_viz_feat_size)

        self.node_sem_feat_projector = torch.nn.Linear(input_sem_feat_size, final_sem_feat_size)
        self.edge_sem_feat_projector = torch.nn.Linear(input_sem_feat_size, final_sem_feat_size)

        self.graph_feat_projected_dim = final_viz_feat_size + final_sem_feat_size

        # construct gnn
        gnn_cfg.input_dim_node = self.graph_feat_projected_dim
        gnn_cfg.input_dim_edge = self.graph_feat_projected_dim
        self.gnn = MODELS.build(gnn_cfg)

        # img feat params
        self.use_img_feats = use_img_feats
        self.img_feat_key = img_feat_key
        self.img_feat_projector = torch.nn.Linear(img_feat_size, self.graph_feat_projected_dim)

        # predictor, loss params
        if isinstance(loss, list):
            # losses
            self.loss_fn = torch.nn.ModuleList([MODELS.build(l) for l in loss])

            # predictors
            dim_list = [gnn_cfg.input_dim_node] * num_predictor_layers
            self.ds_predictor_head = build_mlp(dim_list)
            self.ds_predictor = torch.nn.ModuleList()
            for i in range(3): # separate predictor for each criterion
                self.ds_predictor.append(torch.nn.Linear(gnn_cfg.input_dim_node, num_classes))

        else:
            # loss
            self.loss_fn = MODELS.build(loss)

            # predictor
            dim_list = [gnn_cfg.input_dim_node] * num_predictor_layers + [num_classes]
            self.ds_predictor = build_mlp(dim_list, final_nonlinearity=False)

        self.loss_weight = loss_weight
        self.loss_consensus = loss_consensus

    def predict(self, graph: BaseDataElement, feats: BaseDataElement) -> Tensor:
        # downproject graph feats
        node_feats = []
        edge_feats = []
        if self.final_viz_feat_size > 0:
            node_viz_feats = self.node_viz_feat_projector(graph.nodes.feats[..., :self.input_viz_feat_size])
            edge_viz_feats = self.edge_viz_feat_projector(graph.edges.feats[..., :self.input_viz_feat_size])
            node_feats.append(node_viz_feats)
            edge_feats.append(edge_viz_feats)

        if self.final_sem_feat_size > 0:
            node_sem_feats = self.node_sem_feat_projector(graph.nodes.feats[..., -self.input_sem_feat_size:])
            edge_sem_feats = self.edge_sem_feat_projector(graph.edges.feats[..., -self.input_sem_feat_size:])
            node_feats.append(node_sem_feats)
            edge_feats.append(edge_sem_feats)

        if len(node_feats) == 0 or len(edge_feats) == 0:
            raise ValueError("Sum of final_viz_feat_size and final_sem_feat_size must be > 0")

        graph.nodes.feats = torch.cat(node_feats, -1)
        graph.edges.feats = torch.cat(edge_feats, -1)
        dgl_g = self.gnn(graph)

        # get node features and pool to get graph feats
        orig_node_feats = torch.cat([f[:npi] for f, npi in zip(graph.nodes.feats, graph.nodes.nodes_per_img)])
        node_feats = dgl_g.ndata['feats'] + orig_node_feats # skip connection
        npi_tensor = Tensor(graph.nodes.nodes_per_img).int()
        node_to_img = torch.arange(len(npi_tensor)).repeat_interleave(
                npi_tensor).long().to(node_feats.device)
        graph_feats = torch.zeros(npi_tensor.shape[0], node_feats.shape[-1]).to(node_feats.device)
        scatter_mean(node_feats, node_to_img, dim=0, out=graph_feats)

        # combine two types of feats
        if self.use_img_feats:
            # get img feats
            img_feats = feats.bb_feats[-1] if self.img_feat_key == 'bb' else feats.fpn_feats[-1]
            img_feats = self.img_feat_projector(F.adaptive_avg_pool2d(img_feats,
                1).squeeze(-1).squeeze(-1))
            final_feats = img_feats + graph_feats
        else:
            final_feats = graph_feats

        if isinstance(self.ds_predictor, torch.nn.ModuleList):
            ds_feats = self.ds_predictor_head(final_feats)
            ds_preds = torch.stack([p(ds_feats) for p in self.ds_predictor], 1)
        else:
            ds_preds = self.ds_predictor(final_feats)

        return ds_preds

    def loss(self, graph: BaseDataElement, feats: BaseDataElement,
            batch_data_samples: SampleList) -> Tensor:
        ds_preds = self.predict(graph, feats)
        ds_gt = torch.stack([torch.from_numpy(b.ds) for b in batch_data_samples]).to(ds_preds.device)

        if self.loss_consensus == 'mode':
            ds_gt = ds_gt.float().round().long()
        else:
            ds_gt = ds_gt.long()

        if isinstance(self.loss_fn, torch.nn.ModuleList):
            # compute loss for each criterion and sum
            ds_loss = sum([self.loss_fn[i](ds_preds[:, i], ds_gt[:, i]) for i in range(len(self.loss_fn))]) / len(self.loss_fn)

        else:
            ds_loss = self.loss_fn(ds_preds, ds_gt)

        loss = {'ds_loss': ds_loss * self.loss_weight}

        return loss

@MODELS.register_module()
class STDSHead(DSHead):
    def __init__(self, graph_pooling_window: int = 1, use_temporal_model: bool = False,
            temporal_arch: str = 'transformer', pred_per_frame: bool = False,
            use_node_positional_embedding: bool = True, use_positional_embedding: bool = True,
            **kwargs) -> None:
        super().__init__(**kwargs)
        self.use_temporal_model = use_temporal_model
        self.graph_pooling_window = graph_pooling_window
        self.pred_per_frame = pred_per_frame

        # positional embedding
        self.use_node_positional_embedding = use_node_positional_embedding
        self.use_positional_embedding = use_positional_embedding
        if self.use_node_positional_embedding:
            self.node_pe = PositionalEncoding(self.graph_feat_projected_dim,
                    batch_first=True, return_enc_only=True, dropout=0)

        if self.use_positional_embedding:
            self.pe = PositionalEncoding(self.img_feat_size, batch_first=True,
                    return_enc_only=True, dropout=0)

        # TODO construct temporal model

    def predict(self, graph: BaseDataElement, feats: BaseDataElement,
            results: SampleList = None) -> Tensor:
        # get dims
        B, T, N, _ = graph.nodes.feats.shape

        # downproject graph feats
        node_feats = []
        edge_feats = []
        if self.final_viz_feat_size > 0:
            node_viz_feats = self.node_viz_feat_projector(graph.nodes.feats[..., :self.input_viz_feat_size])
            edge_viz_feats = self.edge_viz_feat_projector(torch.cat(graph.edges.feats)[..., :self.input_viz_feat_size])
            node_feats.append(node_viz_feats)
            edge_feats.append(edge_viz_feats)

        if self.final_sem_feat_size > 0:
            node_sem_feats = self.node_sem_feat_projector(graph.nodes.feats[..., -self.input_sem_feat_size:])
            edge_sem_feats = self.edge_sem_feat_projector(torch.cat(graph.edges.feats)[..., -self.input_sem_feat_size:])
            node_feats.append(node_sem_feats)
            edge_feats.append(edge_sem_feats)

        if len(node_feats) == 0 or len(edge_feats) == 0:
            raise ValueError("Sum of final_viz_feat_size and final_sem_feat_size must be > 0")

        # add positional embedding to node feats
        node_feats = torch.cat(node_feats, -1)

        if self.use_node_positional_embedding:
            # use node_to_fic_id to arrange pos_embeds
            pos_embed = self.node_pe(torch.zeros(1, T, node_feats.shape[-1]).to(node_feats.device))
            node_to_fic_id = torch.arange(5).view(1, -1, 1).repeat(B, 1, N)
            pos_embed_scattered = pos_embed.squeeze()[node_to_fic_id]

            # add to node_feats
            node_feats = F.dropout(node_feats + pos_embed_scattered, 0.1,
                    training=self.training)

        graph.nodes.feats = node_feats
        dgl_g = self.gnn(graph)

        # get node features and pool to get graph feats
        orig_node_feats = torch.cat([torch.cat([f[:n] for f, n in zip(cf, npi.int())]) \
                for cf, npi in zip(graph.nodes.feats, graph.nodes.nodes_per_img)])
        node_feats = dgl_g.ndata['feats'] + orig_node_feats # skip connection

        # pool node feats by img
        node_to_img = torch.cat([ind * T + torch.arange(T).repeat_interleave(n.int()).long().to(
            node_feats.device) for ind, n in enumerate(graph.nodes.nodes_per_img)])
        graph_feats = torch.zeros(B * T, node_feats.shape[-1]).to(node_feats.device)
        scatter_mean(node_feats, node_to_img, dim=0, out=graph_feats)

        # combine two types of feats
        if self.use_img_feats:
            # get img feats
            img_feats = feats.bb_feats[-1] if self.img_feat_key == 'bb' else feats.fpn_feats[-1]
            img_feats = F.adaptive_avg_pool2d(img_feats, 1).squeeze(-1).squeeze(-1)

            if self.use_temporal_model:
                img_feats = self.img_feat_temporal_model(img_feats)

            elif self.use_positional_embedding:
                pos_embed = self.pe(torch.zeros(1, T, img_feats.shape[-1]).to(img_feats.device))
                img_feats = F.dropout(img_feats + pos_embed, 0.1, training=self.training)

            # downproject img feats
            projected_img_feats = self.img_feat_projector(img_feats)
            final_feats = graph_feats.view(B, T, -1) + projected_img_feats

        # 2 modes: 1 prediction per clip for clip classification, or output per-keyframe for
        # whole-video inputs

        # pred-per-frame handles the second case, but can also apply to clip classification
        # during training, in case we still want per-frame output for clip classification
        if self.pred_per_frame:
            ds_preds = self._ds_predict(final_feats.flatten(end_dim=1)).view(B, T,
                    *final_feats.shape[2:])

            if not self.training:
                # filter preds for keyframes during evaluation
                breakpoint()
                ds_preds = [x for r, x in zip(results, ds_preds.flatten(end_dim=1)) \
                        if r['is_ds_keyframe']]

        else:
            if self.graph_pooling_window == -1:
                self.graph_pooling_window = T # keep all frame feats

            # pool based on pooling window
            final_feats = final_feats[:, -self.graph_pooling_window:].mean(1)
            ds_preds = self._ds_predict(final_feats)

        return ds_preds

    def loss(self, graph: BaseDataElement, feats: BaseDataElement,
            batch_data_samples: SampleList) -> Tensor:
        ds_preds = self.predict(graph, feats)
        ds_gt = torch.stack([torch.stack([torch.from_numpy(b.ds) for b in vds.video_data_samples]) \
                for vds in batch_data_samples]).to(ds_preds.device)

        # preprocess gt
        if self.loss_consensus == 'mode':
            ds_gt = ds_gt.float().round().long()
        else:
            ds_gt = ds_gt.long()

        # TODO handle case when loss_fn is module list (multi-task ds head)
        # reshape preds and gt according to prediction settings
        if not self.pred_per_frame:
            # keep only last gt per clip
            ds_gt = ds_gt[:, -1]
        else:
            ds_preds = ds_preds.flatten()
            ds_gt = ds_gt.flatten()

        if isinstance(self.loss_fn, torch.nn.ModuleList):
            # compute loss for each criterion and sum
            ds_loss = sum([self.loss_fn[i](ds_preds[:, i], ds_gt[:, i]) for i in range(len(self.loss_fn))]) / len(self.loss_fn)

        else:
            ds_loss = self.loss_fn(ds_preds, ds_gt)

        loss = {'ds_loss': ds_loss * self.loss_weight}

        return loss

    def _ds_predict(self, final_feats):
        if isinstance(self.ds_predictor, torch.nn.ModuleList):
            ds_feats = self.ds_predictor_head(final_feats)
            ds_preds = torch.stack([p(ds_feats) for p in self.ds_predictor], 1)
        else:
            ds_preds = self.ds_predictor(final_feats)

        return ds_preds
