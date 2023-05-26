"""
 Copyright (C) 2020-2023 Intel Corporation

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import math

import cv2
import numpy as np


class Detection:
    def __init__(self, xmin, ymin, xmax, ymax, score, id, str_label=None):
        self.xmin = xmin
        self.ymin = ymin
        self.xmax = xmax
        self.ymax = ymax
        self.score = score
        self.id = int(id)
        self.str_label = str_label

    def get_coords(self):
        return self.xmin, self.ymin, self.xmax, self.ymax

    def __to_str(self):
        return f"({self.xmin}, {self.ymin}, {self.xmax}, {self.ymax}, {self.score:.3f}, {self.id}, {self.str_label})"

    def __str__(self):
        return self.__to_str()

    def __repr__(self):
        return self.__to_str()


class SegmentedObject(Detection):
    def __init__(self, xmin, ymin, xmax, ymax, score, id, str_label, mask):
        super().__init__(xmin, ymin, xmax, ymax, score, id, str_label)
        self.mask = mask

    def __to_str(self):
        return f"({self.xmin}, {self.ymin}, {self.xmax}, {self.ymax}, {self.score:.3f}, {self.id}, {self.str_label}, {(self.mask > 0.5).sum()})"

    def __str__(self):
        return self.__to_str()

    def __repr__(self):
        return self.__to_str()


class SegmentedObjectWithRects(SegmentedObject):
    def __init__(self, segmented_object):
        super().__init__(
            segmented_object.xmin,
            segmented_object.ymin,
            segmented_object.xmax,
            segmented_object.ymax,
            segmented_object.score,
            segmented_object.id,
            segmented_object.str_label,
            segmented_object.mask,
        )
        self.rotated_rects = []

    def __to_str(self):
        res = f"({self.xmin}, {self.ymin}, {self.xmax}, {self.ymax}, {self.score:.3f}, {self.id}, {self.str_label}, {(self.mask > 0.5).sum()}"
        for rect in self.rotated_rects:
            res += f", RotatedRect: {rect[0][0]:.3f} {rect[0][1]:.3f} {rect[1][0]:.3f} {rect[1][1]:.3f} {rect[2]:.3f}"
        return res + ")"

    def __str__(self):
        return self.__to_str()

    def __repr__(self):
        return self.__to_str()


def add_rotated_rects(segmented_objects):
    objects_with_rects = []
    for segmented_object in segmented_objects:
        objects_with_rects.append(SegmentedObjectWithRects(segmented_object))
        mask = segmented_object.mask.astype(np.uint8)
        contours, hierarchies = cv2.findContours(
            mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
        )
        if hierarchies is None:
            continue
        for contour, hierarchy in zip(contours, hierarchies[0]):
            if hierarchy[3] != -1:
                continue
            if len(contour) <= 2 or cv2.contourArea(contour) < 1.0:
                continue
            objects_with_rects[-1].rotated_rects.append(cv2.minAreaRect(contour))
    return objects_with_rects


def clip_detections(detections, size):
    for detection in detections:
        detection.xmin = min(max(round(detection.xmin), 0), size[1])
        detection.ymin = min(max(round(detection.ymin), 0), size[0])
        detection.xmax = min(max(round(detection.xmax), 0), size[1])
        detection.ymax = min(max(round(detection.ymax), 0), size[0])
    return detections


class DetectionWithLandmarks(Detection):
    def __init__(self, xmin, ymin, xmax, ymax, score, id, landmarks_x, landmarks_y):
        super().__init__(xmin, ymin, xmax, ymax, score, id)
        self.landmarks = []
        for x, y in zip(landmarks_x, landmarks_y):
            self.landmarks.append((x, y))


class OutputTransform:
    def __init__(self, input_size, output_resolution):
        self.output_resolution = output_resolution
        if self.output_resolution:
            self.new_resolution = self.compute_resolution(input_size)

    def compute_resolution(self, input_size):
        self.input_size = input_size
        size = self.input_size[::-1]
        self.scale_factor = min(
            self.output_resolution[0] / size[0], self.output_resolution[1] / size[1]
        )
        return self.scale(size)

    def resize(self, image):
        if not self.output_resolution:
            return image
        curr_size = image.shape[:2]
        if curr_size != self.input_size:
            self.new_resolution = self.compute_resolution(curr_size)
        if self.scale_factor == 1:
            return image
        return cv2.resize(image, self.new_resolution)

    def scale(self, inputs):
        if not self.output_resolution or self.scale_factor == 1:
            return inputs
        return (np.array(inputs) * self.scale_factor).astype(np.int32)


class InputTransform:
    def __init__(
        self, reverse_input_channels=False, mean_values=None, scale_values=None
    ):
        self.reverse_input_channels = reverse_input_channels
        self.is_trivial = not (reverse_input_channels or mean_values or scale_values)
        self.means = (
            np.array(mean_values, dtype=np.float32)
            if mean_values
            else np.array([0.0, 0.0, 0.0])
        )
        self.std_scales = (
            np.array(scale_values, dtype=np.float32)
            if scale_values
            else np.array([1.0, 1.0, 1.0])
        )

    def __call__(self, inputs):
        if self.is_trivial:
            return inputs
        if self.reverse_input_channels:
            inputs = cv2.cvtColor(inputs, cv2.COLOR_BGR2RGB)
        return (inputs - self.means) / self.std_scales


def load_labels(label_file):
    with open(label_file, "r") as f:
        labels_map = [x.strip() for x in f]
    return labels_map


def resize_image(image, size, keep_aspect_ratio=False, interpolation=cv2.INTER_LINEAR):
    if keep_aspect_ratio:
        h, w = image.shape[:2]
        scale = min(size[1] / h, size[0] / w)
        return cv2.resize(image, None, fx=scale, fy=scale, interpolation=interpolation)
    return cv2.resize(image, size, interpolation=interpolation)


def resize_image_with_aspect(image, size, interpolation=cv2.INTER_LINEAR):
    return resize_image(
        image, size, keep_aspect_ratio=True, interpolation=interpolation
    )


def resize_image_letterbox(image, size, interpolation=cv2.INTER_LINEAR, pad_value=0):
    ih, iw = image.shape[0:2]
    w, h = size
    scale = min(w / iw, h / ih)
    nw = round(iw * scale)
    nh = round(ih * scale)
    image = cv2.resize(image, (nw, nh), interpolation=interpolation)
    dx = (w - nw) // 2
    dy = (h - nh) // 2
    return np.pad(
        image,
        ((dy, h - nh - dy), (dx, w - nw - dx), (0, 0)),
        mode="constant",
        constant_values=pad_value,
    )


def crop_resize(image, size):
    desired_aspect_ratio = size[1] / size[0]  # width / height
    if desired_aspect_ratio == 1:
        if image.shape[0] > image.shape[1]:
            offset = (image.shape[0] - image.shape[1]) // 2
            cropped_frame = image[offset : image.shape[1] + offset]
        else:
            offset = (image.shape[1] - image.shape[0]) // 2
            cropped_frame = image[:, offset : image.shape[0] + offset]
    elif desired_aspect_ratio < 1:
        new_width = math.floor(image.shape[0] * desired_aspect_ratio)
        offset = (image.shape[1] - new_width) // 2
        cropped_frame = image[:, offset : new_width + offset]
    elif desired_aspect_ratio > 1:
        new_height = math.floor(image.shape[1] / desired_aspect_ratio)
        offset = (image.shape[0] - new_height) // 2
        cropped_frame = image[offset : new_height + offset]

    return cv2.resize(cropped_frame, size)


RESIZE_TYPES = {
    "crop": crop_resize,
    "standard": resize_image,
    "fit_to_window": resize_image_with_aspect,
    "fit_to_window_letterbox": resize_image_letterbox,
}


INTERPOLATION_TYPES = {
    "LINEAR": cv2.INTER_LINEAR,
    "CUBIC": cv2.INTER_CUBIC,
    "NEAREST": cv2.INTER_NEAREST,
    "AREA": cv2.INTER_AREA,
}


def nms(x1, y1, x2, y2, scores, thresh, include_boundaries=False, keep_top_k=None):
    b = 1 if include_boundaries else 0
    areas = (x2 - x1 + b) * (y2 - y1 + b)
    order = scores.argsort()[::-1]

    if keep_top_k:
        order = order[:keep_top_k]

    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1 + b)
        h = np.maximum(0.0, yy2 - yy1 + b)
        intersection = w * h

        union = areas[i] + areas[order[1:]] - intersection
        overlap = np.zeros_like(intersection, dtype=float)
        overlap = np.divide(
            intersection,
            union,
            out=overlap,
            where=union != 0,
        )

        order = order[np.where(overlap <= thresh)[0] + 1]

    return keep


def softmax(logits, axis=None, keepdims=False):
    exp = np.exp(logits - np.max(logits))
    return exp / np.sum(exp, axis=axis, keepdims=keepdims)
