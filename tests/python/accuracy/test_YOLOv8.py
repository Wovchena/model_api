import functools
import os
from pathlib import Path

import cv2
import numpy as np
import openvino.runtime as ov
import pytest
from openvino.model_api.models import YOLOv5
import ultralytics
import torch
import types


def _init_predictor(yolo):
    yolo.predict(np.empty([1, 1, 3], np.uint8))


@functools.lru_cache(maxsize=1)
def _cached_models(pt):
    export_dir = Path(
        ultralytics.YOLO(Path(os.environ["DATA"]) / "ultralytics" / pt, "detect").export(format="openvino", half=True)
    )
    impl_wrapper = YOLOv5.create_model(export_dir / (pt.with_suffix(".xml")), device="CPU")
    ref_wrapper = ultralytics.YOLO(export_dir, "detect")
    ref_wrapper.overrides["imgsz"] = (impl_wrapper.w, impl_wrapper.h)
    _init_predictor(ref_wrapper)
    ref_wrapper.predictor.model.ov_compiled_model = ov.Core().compile_model(
        ref_wrapper.predictor.model.ov_model, "CPU"
    )
    ref_dir = export_dir / "ref"
    ref_dir.mkdir(exist_ok=True)
    return impl_wrapper, ref_wrapper, ref_dir


def _impaths(all):
    """
    It's impossible to pass fixture as argument for
    @pytest.mark.parametrize, so it can't take a cmd arg. Use env var
    instead. Another solution was to define
    pytest_generate_tests(metafunc) in conftest.py
    """
    impaths = sorted(
        file
        for file in (Path(os.environ["DATA"]) / "coco128/images/train2017/").iterdir()
        if all or file.name
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


@pytest.mark.parametrize("impath", _impaths(all=False))
@pytest.mark.parametrize("pt", [Path("yolov5mu.pt"), Path("yolov8l.pt")])
def test_accuracy_detector(impath, pt):
    impl_wrapper, ref_wrapper, ref_dir = _cached_models(pt)
    im = cv2.imread(str(impath))
    assert im is not None
    impl_preds = impl_wrapper(im)
    pred_boxes = np.array(
        [
            (
                impl_pred.xmin,
                impl_pred.ymin,
                impl_pred.xmax,
                impl_pred.ymax,
                impl_pred.score,
                impl_pred.id,
            )
            for impl_pred in impl_preds.objects
        ],
        dtype=np.float32,
    )
    ref_predictions = ref_wrapper.predict(im)
    assert 1 == len(ref_predictions)
    ref_boxes = ref_predictions[0].boxes.data.numpy()
    if 0 == pred_boxes.size == ref_boxes.size:
        return  # np.isclose() doesn't work for empty arrays
    ref_boxes[:, :4] = np.round(ref_boxes[:, :4], out=ref_boxes[:, :4])
    assert np.isclose(
        pred_boxes[:, :4], ref_boxes[:, :4], 0, 1
    ).all()  # Allow one pixel deviation because image preprocessing is imbedded into the model
    assert np.isclose(pred_boxes[:, 4], ref_boxes[:, 4], 0.0, 0.02).all()
    assert (pred_boxes[:, 5] == ref_boxes[:, 5]).all()
    with open(ref_dir / impath.with_suffix(".txt").name, "w") as file:
        print(impl_preds, end="", file=file)


class Metrics(ultralytics.models.yolo.detect.DetectionValidator):
    @torch.inference_mode()
    def evaluate(self, yolo, dataset_yaml):
        # TODO: multilabel, both scales, ceil instead of round
        self.data = ultralytics.data.utils.check_det_dataset(dataset_yaml)
        dataloader = self.get_dataloader(self.data[self.args.split], batch_size=1)
        dataloader.dataset.transforms.transforms = (lambda di: {
            'batch_idx': torch.zeros(len(di['instances'])),
            'bboxes': torch.from_numpy(di['instances'].bboxes),
            'cls': torch.from_numpy(di['cls']),
            'img': torch.empty(1, 1, 1),
            'im_file': di['im_file'],
            'ori_shape': di['ori_shape'],
            'ratio_pad': [(1.0, 1.0), (0, 0)],
        },)
        self.init_metrics(types.SimpleNamespace(names={idx: label for idx, label in enumerate(yolo.labels)}))
        for batch in dataloader:
            im = cv2.imread(batch['im_file'][0])
            # pred = torch.tensor(
            #     [[
            #         (
            #             impl_pred.xmin / im.shape[1],
            #             impl_pred.ymin / im.shape[0],
            #             impl_pred.xmax / im.shape[1],
            #             impl_pred.ymax / im.shape[0],
            #             impl_pred.score,
            #             impl_pred.id,
            #         )
            #         for impl_pred in yolo(im).objects
            #     ]],
            #     dtype=torch.float32,
            # )
            # if not pred.numel():
            #     pred = torch.empty(1, 0, 6)
            pred = yolo.predict(im, conf=0.001)[0].boxes.data
            pred[:, (0, 2)] /= im.shape[1]
            pred[:, (1, 3)] /= im.shape[0]
            self.update_metrics([pred], batch)
        return self.get_stats()


@functools.lru_cache(maxsize=1)
def _cached_detector_metric(pt):
    export_dir = export_dir = Path(
        ultralytics.YOLO(Path(os.environ["DATA"]) / "ultralytics" / pt, "detect").export(format="openvino", half=True)
    )
    yolo = YOLOv5.create_model(export_dir / Path(pt).with_suffix(".xml"), device="CPU", configuration={"confidence_threshold": 0.001})
    ref = ultralytics.YOLO(export_dir, "detect")
    ref.overrides = {"imgsz": (yolo.w, yolo.h), "verbose": False, "save": False, "show": False}
    ref.labels = yolo.labels
    return ref


@pytest.mark.parametrize("pt,dataset_yaml,ref_mAP50_95", [
    ("coco8.yaml", 0.609230627349896747269042407424421980977058410644531250000000000000000000000000000000000000000000000, Path("yolov8n.pt")),
    # ("coco128.yaml", 0.439413760605130543357432770790182985365390777587890625000000000000000000000000000000000000000000000, Path("yolov8n.pt")),
    # # target: 0.453295803020367371605203743456513620913028717041015625000000000000000000000000000000000000000000000
    # ("coco.yaml", 0.365912774507280713631729440749040804803371429443359375000000000000000000000000000000000000000000000, Path("yolov8n.pt")),
    # # target: 0.371225813018740136151052411150885745882987976074218750000000000000000000000000000000000000000000000
#     r("yolov5n6u.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # DetectionValidator round() or ceil(): 0.564731311936565338882587639091070741415023803710937500000000000000000000000000000000000000000000000
#     # round(): 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000
#     # ceil(): 0.656271644963383637971787720744032412767410278320312500000000000000000000000000000000000000000000000
#     c("yolov5n6u.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # DetectionValidator round() or ceil(): 0.447109695314321209380636901187244802713394165039062500000000000000000000000000000000000000000000000
#     # round(): 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000
#     # ceil(): 0.487694517974101959811861206617322750389575958251953125000000000000000000000000000000000000000000000
# r    r("yolov5n6u.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # DetectionValidator round() 0.359585325993989290971342143166111782193183898925781250000000000000000000000000000000000000000000000
#     # round(): 0.417544860140942553083931443325127474963665008544921875000000000000000000000000000000000000000000000
#     # ceil(): 0.417306123595609090859426260067266412079334259033203125000000000000000000000000000000000000000000000
#     d("yolov5nu.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.623142979161427690293351133732357993721961975097656250000000000000000000000000000000000000000000000
#     # ceil(): 0.623142979161427690293351133732357993721961975097656250000000000000000000000000000000000000000000000
#     r("yolov5nu.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.414251514921500862964620637285406701266765594482421875000000000000000000000000000000000000000000000
#     # ceil(): 0.413331711316410554957201384240761399269104003906250000000000000000000000000000000000000000000000000
# r    r("yolov5nu.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.340669530427118116833185013092588633298873901367187500000000000000000000000000000000000000000000000
#     # ceil(): 0.340581889744006383047292274568462744355201721191406250000000000000000000000000000000000000000000000
#     d("yolov5su.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.647105462460407232505588126514339819550514221191406250000000000000000000000000000000000000000000000
#     # ceil(): 0.647105462460407232505588126514339819550514221191406250000000000000000000000000000000000000000000000
#     r("yolov5su.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.561486919361370961922830247203819453716278076171875000000000000000000000000000000000000000000000000
#     # ceil(): 0.560886186232607886203993530216393992304801940917968750000000000000000000000000000000000000000000000
# c    c("yolov5su.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.427056839673187471628779121601837687194347381591796875000000000000000000000000000000000000000000000
#     # ceil(): 0.427237280426085719309270416488288901746273040771484375000000000000000000000000000000000000000000000
#     c("yolov5s6u.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.683232506476166068232203087973175570368766784667968750000000000000000000000000000000000000000000000
#     # ceil(): 0.701730272156686152307258907967479899525642395019531250000000000000000000000000000000000000000000000
#     c("yolov5s6u.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.615457952350294323906609861296601593494415283203125000000000000000000000000000000000000000000000000
#     # ceil(): 0.615911691320302101537720318447099998593330383300781250000000000000000000000000000000000000000000000
# r    r("yolov5s6u.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.483630464360948475466273066558642312884330749511718750000000000000000000000000000000000000000000000
#     # ceil(): 0.483440422795322677362861440997221507132053375244140625000000000000000000000000000000000000000000000
#     d("yolov8s.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.713630628137169598090849831351079046726226806640625000000000000000000000000000000000000000000000000
#     # ceil(): 0.713630628137169598090849831351079046726226806640625000000000000000000000000000000000000000000000000
#     r("yolov8s.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.597909910154621915268080556415952742099761962890625000000000000000000000000000000000000000000000000
#     # ceil(): 0.597813347954974871889533005742123350501060485839843750000000000000000000000000000000000000000000000
# c    c("yolov8s.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.447692743139664617313400185594218783080577850341796875000000000000000000000000000000000000000000000
#     # ceil(): 0.448147623722066845708411619852995499968528747558593750000000000000000000000000000000000000000000000

#     d("yolov8m.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.728461706662819619140236682142131030559539794921875000000000000000000000000000000000000000000000000
#     # ceil(): 0.728461706662819619140236682142131030559539794921875000000000000000000000000000000000000000000000000
#     c("yolov8m.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.612390365612367926217984859249554574489593505859375000000000000000000000000000000000000000000000000
#     # ceil(): 0.612751770262545569778467324795201420783996582031250000000000000000000000000000000000000000000000000
# r    r("yolov8m.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.501310503617516500796114087279420346021652221679687500000000000000000000000000000000000000000000000
#     # ceil(): 0.501231720467534835883327559713507071137428283691406250000000000000000000000000000000000000000000000
#     d("yolov5mu.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.723701379209853556950804431835422292351722717285156250000000000000000000000000000000000000000000000
#     # ceil(): 0.723701379209853556950804431835422292351722717285156250000000000000000000000000000000000000000000000
#     r("yolov5mu.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.599828632536987260692740164813585579395294189453125000000000000000000000000000000000000000000000000
#     # ceil(): 0.599535557313378597577013806585455313324928283691406250000000000000000000000000000000000000000000000
# r    r("yolov5mu.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.487646669748335315208720430746325291693210601806640625000000000000000000000000000000000000000000000
#     # ceil(): 0.487439467695613026787526678162976168096065521240234375000000000000000000000000000000000000000000000
#     c("yolov5m6u.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.817060053968598709595028140029171481728553771972656250000000000000000000000000000000000000000000000
#     # ceil(): 0.817762235449735253034475590538932010531425476074218750000000000000000000000000000000000000000000000
#     r("yolov5m6u.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.671334921952266960865074452158296480774879455566406250000000000000000000000000000000000000000000000
#     # ceil(): 0.671091401257249420275741158548044040799140930175781250000000000000000000000000000000000000000000000
# r    r("yolov5m6u.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.532970924919012656317818255047313868999481201171875000000000000000000000000000000000000000000000000
#     # ceil(): 0.533013840137534722352086191676789894700050354003906250000000000000000000000000000000000000000000000

#     d("yolov8l.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.755419895508826488850218083825893700122833251953125000000000000000000000000000000000000000000000000
#     # ceil(): 0.755419895508826488850218083825893700122833251953125000000000000000000000000000000000000000000000000
#     c("yolov8l.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.658809076455488584755926240177359431982040405273437500000000000000000000000000000000000000000000000
#     # ceil(): 0.660095034211136244550743867876008152961730957031250000000000000000000000000000000000000000000000000
# r    r("yolov8l.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.527952025095862254033818317111581563949584960937500000000000000000000000000000000000000000000000000
#     # ceil(): 0.527926452487104458377586979622719809412956237792968750000000000000000000000000000000000000000000000
#     d("yolov5lu.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.738387777295164693391882337891729548573493957519531250000000000000000000000000000000000000000000000
#     # ceil(): 0.738387777295164693391882337891729548573493957519531250000000000000000000000000000000000000000000000
#     c("yolov5lu.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.646041302000092132473696437955368310213088989257812500000000000000000000000000000000000000000000000
#     # ceil(): 0.646750272261074732327301717305090278387069702148437500000000000000000000000000000000000000000000000
# r    r("yolov5lu.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.520639674720017486819756413751747459173202514648437500000000000000000000000000000000000000000000000
#     # ceil(): 0.520620207506766186078550617821747437119483947753906250000000000000000000000000000000000000000000000
#     r("yolov5l6u.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.816985385626335935960184997384203597903251647949218750000000000000000000000000000000000000000000000
#     # ceil(): 0.799586799697671657405351197667187079787254333496093750000000000000000000000000000000000000000000000
#     c("yolov5l6u.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.702240098718037941694092296529561281204223632812500000000000000000000000000000000000000000000000000
#     # ceil(): 0.702666447399680649255060416180640459060668945312500000000000000000000000000000000000000000000000000
# r    r("yolov5l6u.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.553796178424930340966625408327672630548477172851562500000000000000000000000000000000000000000000000
#     # ceil(): 0.553726263977916355329966791032347828149795532226562500000000000000000000000000000000000000000000000

#     d("yolov8x.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.747160638555676603900224108656402677297592163085937500000000000000000000000000000000000000000000000
#     # ceil(): 0.747160638555676603900224108656402677297592163085937500000000000000000000000000000000000000000000000
#     c("yolov8x.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.667443243945732955779703843290917575359344482421875000000000000000000000000000000000000000000000000
#     # ceil(): 0.666910929459220480630676775035681203007698059082031250000000000000000000000000000000000000000000000
# r    r("yolov8x.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.538576370207422439051470064441673457622528076171875000000000000000000000000000000000000000000000000
#     # ceil(): 0.538538732080184989747806412196950986981391906738281250000000000000000000000000000000000000000000000
#     d("yolov5xu.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.769995194981170416603788453357992693781852722167968750000000000000000000000000000000000000000000000
#     # ceil(): 0.769995194981170416603788453357992693781852722167968750000000000000000000000000000000000000000000000
#     ("yolov5xu.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.657296764072909822651524791581323370337486267089843750000000000000000000000000000000000000000000000
#     # missing
# r    r("yolov5xu.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # round(): 0.530579423309684328202706637966912239789962768554687500000000000000000000000000000000000000000000000
#     # ceil(): 0.530548124128754183814749012526590377092361450195312500000000000000000000000000000000000000000000000
#     r("yolov5x6u.pt", "coco8.yaml", 0.641611753875985235673340412176912650465965270996093750000000000000000000000000000000000000000000000),
#     # round(): 0.807242729134664549484057260997360572218894958496093750000000000000000000000000000000000000000000000
#     # ceil(): 0.789352837514934235763064407365163788199424743652343750000000000000000000000000000000000000000000000
# c    c("yolov5x6u.pt", "coco128.yaml", 0.487201415947649429938337561907246708869934082031250000000000000000000000000000000000000000000000000),
#     # round(): 0.708807789520117803583332261041505262255668640136718750000000000000000000000000000000000000000000000
#     # ceil(): 0.709211013006002755076906396425329148769378662109375000000000000000000000000000000000000000000000000
#     ("yolov5x6u.pt", "coco.yaml", 0.411201798889327063690757313452195376157760620117187500000000000000000000000000000000000000000000000),
#     # missing
])
def test_detector_metric(pt, dataset_yaml, ref_mAP50_95):
    from ultralytics import YOLO
    model = YOLO('yolov8n.pt')
    results = model.val(data='coco.yaml')
    print(results)
    # # mAP50_95 = ultralytics.models.yolo.detect.DetectionValidator(args={"data": dataset_yaml})(model=pt)["metrics/mAP50-95(B)"]
    # # print(f"{mAP50_95:.99f}")
    # # assert mAP50_95 >= ref_mAP50_95
    # yolo = _cached_detector_metric(pt)
    # mAP50_95 = Metrics().evaluate(yolo, dataset_yaml)["metrics/mAP50-95(B)"]
    # print(f"{mAP50_95:.99f}")
    # # assert mAP50_95 >= ref_mAP50_95
