import os
import copy

# modify base for different detectors
_base_ = [
    '../lg_ds_base.py', os.path.expandvars('$MMDETECTION/configs/_base_/models/mask-rcnn_r50_fpn.py'),
]

# extract detector, data preprocessor config from base
detector = copy.deepcopy(_base_.model)
detector.roi_head.bbox_head.num_classes = _base_.num_classes
detector.roi_head.mask_head.num_classes = _base_.num_classes
detector.test_cfg.rcnn.max_per_img = _base_.num_nodes
dp = copy.deepcopy(_base_.model.data_preprocessor)
dp.pad_size_divisor = 1
dp.pad_mask = False
del _base_.model
del detector.data_preprocessor

# extract lg config, set detector
model = copy.deepcopy(_base_.lg_model)
model.data_preprocessor = dp
model.detector = detector
model.reconstruction_img_stats=dict(mean=dp.mean, std=dp.std)
model.roi_extractor = copy.deepcopy(detector.roi_head.bbox_roi_extractor)
model.roi_extractor.roi_layer.output_size = 1

# trainable bb, neck
model.trainable_backbone_cfg = None

del _base_.lg_model

# modify load_from
load_from = _base_.load_from.replace('base', 'mask_rcnn')

# remove visual features
model.ds_head.final_viz_feat_size = 0
model.ds_head.use_img_feats = False

# semantic modifications
#model.semantic_feat_size = 512
#model.graph_head.semantic_feat_size = 512
#model.ds_head.input_sem_feat_size = 512
#model.ds_head.final_sem_feat_size = 512
#model.use_semantic_queries = True

# train graph head since we are changing semantic feat projector arch, use pred detections rather than gt
#model.force_train_graph_head = True
#model.graph_head.presence_loss_weight = 1
#model.graph_head.classifier_loss_weight = 1

# optimizer
optim_wrapper = dict(
    optimizer=dict(lr=0.0001),
)

train_cfg = dict(
    type='EpochBasedTrainLoop',
    max_epochs=30,
    val_interval=1)
