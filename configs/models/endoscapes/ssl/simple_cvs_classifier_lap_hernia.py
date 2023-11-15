import os

_base_ = ['../simple_cvs_classifier.py']

model = dict(
    backbone=dict(
        init_cfg=dict(
            checkpoint='weights/ssl_weights/moco/converted_lap_hernia.torch',
        ),
    ),
)
