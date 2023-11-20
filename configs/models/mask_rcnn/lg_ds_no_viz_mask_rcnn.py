_base_ = 'lg_ds_mask_rcnn.py'

model = dict(
    # remove trainable backbone
    trainable_backbone_cfg=None,
    ds_head=dict(
        # remove visual features
        final_viz_feat_size=0,
        use_img_feats = False,
        # turn off perturbation
        semantic_loss_weight=0,
        viz_loss_weight=0,
        img_loss_weight=0,
    ),
    # skip graph head training
    force_train_graph_head=True,
)

# optimizer
optim_wrapper = dict(
    optimizer=dict(lr=0.00001),
    paramwise_cfg=dict(
        custom_keys={
            'semantic_feat_projector': dict(lr_mult=10),
        }
    ),
)

train_cfg = dict(
    type='EpochBasedTrainLoop',
    max_epochs=30,
    val_interval=1)

# freeze graph head (viz feats are frozen, no need to update graph prediction)
custom_hooks = [dict(type="FreezeHook", freeze_graph_head=False)]
