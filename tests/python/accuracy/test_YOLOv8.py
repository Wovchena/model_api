import functools
import os
from distutils.dir_util import copy_tree
from pathlib import Path

import cv2
import numpy as np
import openvino.runtime as ov
import pytest
import torch
import torchvision.transforms as T
from openvino.model_api.models import YOLOv5
from ultralytics import YOLO
from ultralytics.yolo.engine.results import Results


class CenterCrop:
    # YOLOv8 CenterCrop class for image preprocessing, i.e. T.Compose([CenterCrop(size), ToTensor()])
    def __init__(self, size=640):
        """Converts an image from numpy array to PyTorch tensor."""
        super().__init__()
        self.h, self.w = (size, size) if isinstance(size, int) else size

    def __call__(self, im):  # im = np.array HWC
        imh, imw = im.shape[:2]
        m = min(imh, imw)  # min dimension
        top, left = (imh - m) // 2, (imw - m) // 2
        return cv2.resize(
            im[top : top + m, left : left + m],
            (self.w, self.h),
            interpolation=cv2.INTER_LINEAR,
        )


class ToTensor:
    # YOLOv8 ToTensor class for image preprocessing, i.e. T.Compose([LetterBox(size), ToTensor()])
    def __init__(self, half=False):
        """Initialize YOLOv8 ToTensor object with optional half-precision support."""
        super().__init__()
        self.half = half

    def __call__(self, im):  # im = np.array HWC in BGR order
        im = np.ascontiguousarray(
            im.transpose((2, 0, 1))[::-1]
        )  # HWC to CHW -> BGR to RGB -> contiguous
        im = torch.from_numpy(im)  # to torch
        im = im.half() if self.half else im.float()  # uint8 to fp16/32
        im /= 255.0  # 0-255 to 0.0-1.0
        return im


def _init_predictor(yolo):
    yolo.predict(np.empty([1, 1, 3], np.uint8))


@functools.lru_cache(maxsize=1)
def _cached_models(folder, pt):
    pt = Path(pt)
    export_dir = Path(
        YOLO(folder / "ultralytics/detectors" / pt, "detect").export(format="openvino")
    )
    impl_wrapper = YOLOv5.create_model(export_dir / (pt.stem + ".xml"), device="CPU")
    ref_wrapper = YOLO(export_dir, "detect")
    ref_wrapper.overrides["imgsz"] = (impl_wrapper.w, impl_wrapper.h)
    _init_predictor(ref_wrapper)
    ref_wrapper.predictor.model.ov_compiled_model = ov.Core().compile_model(
        ref_wrapper.predictor.model.ov_model, "CPU"
    )
    ref_dir = export_dir / "ref"
    ref_dir.mkdir(exist_ok=True)
    return impl_wrapper, ref_wrapper, ref_dir


def _impaths():
    """
    It's impossible to pass fixture as argument for
    @pytest.mark.parametrize, so I can't take cmd arg. Use env var
    instead. Another solution was to define
    pytest_generate_tests(metafunc) in conftest.py.
    """
    impaths = sorted(
        file
        for file in (Path(os.environ["DATA"]) / "coco128/images/train2017/").iterdir()
        if file.name
        not in {  # This images fail because image preprocessing is imbedded into the model
            "000000000143.jpg",
            "000000000491.jpg",
            "000000000536.jpg",
            "000000000581.jpg",
        }
    )
    if not impaths:
        raise RuntimeError(
            f"{Path(os.environ['DATA']) / 'coco128/images/train2017/'} is empty"
        )
    return impaths


@pytest.mark.parametrize("impath", _impaths())
@pytest.mark.parametrize("pt", ["yolov5mu.pt", "yolov8l.pt"])
def test_detector(impath, data, pt):
    impl_wrapper, ref_wrapper, ref_dir = _cached_models(data, pt)
    im = cv2.imread(str(impath))
    assert im is not None
    impl_preds = impl_wrapper(im)
    pred_boxes = np.array(
        [
            [
                impl_pred.xmin,
                impl_pred.ymin,
                impl_pred.xmax,
                impl_pred.ymax,
                impl_pred.score,
                impl_pred.id,
            ]
            for impl_pred in impl_preds.objects
        ],
        dtype=np.float32,
    )
    ref_predictions = ref_wrapper.predict(im)
    assert 1 == len(ref_predictions)
    ref_boxes = ref_predictions[0].boxes.data.numpy()
    with open(ref_dir / impath.with_suffix(".txt").name, "w") as file:
        print(impl_preds, end="", file=file)
    if 0 == pred_boxes.size == ref_boxes.size:
        return  # np.isclose() doesn't work for empty arrays
    ref_boxes[:, :4] = np.round(ref_boxes[:, :4], out=ref_boxes[:, :4])
    assert np.isclose(
        pred_boxes[:, :4], ref_boxes[:, :4], 0, 1
    ).all()  # Allow one pixel deviation because image preprocessing is imbedded into the model
    assert np.isclose(pred_boxes[:, 4], ref_boxes[:, 4], 0.0, 0.02).all()
    assert (pred_boxes[:, 5] == ref_boxes[:, 5]).all()
