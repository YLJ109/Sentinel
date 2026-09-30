"""视觉推理引擎：YOLO 推理 → 目标解析 → 多目标跟踪 → 行为识别。

三条关键设计：
1. **每路独立管线**：跟踪器与行为分析器按 ``pipeline_key`` 隔离，
   因此多路视频可安全并发推理，互不污染轨迹；视频文件分析使用独立 key，
   不会清掉同一摄像头的实时跟踪状态。
2. **同点位结果共享**：多个观看者看同一路时共享同一份推理结果与限频窗口，
   避免"开 3 个页面 = 推理 3 次"。跳过推理的帧会复用上一次的行为结果，
   保证前端看到的行为框不会闪烁。
3. **运动门控**：画面几乎静止时跳过推理（借鉴 Frigate 的流水线思路），
   空场景（夜间走廊、无人教室）算力占用可大幅下降。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.core.config import settings
from app.core.runtime_config import runtime_config
from app.vision.behaviors import BehaviorAnalyzer, BehaviorHit
from app.vision.emotion import EmotionSmoother, emotion_estimator
from app.vision.face import face_detector
from app.vision.face_id import face_id
from app.vision.registry import registry
from app.vision.tracker import IoUTracker, PeopleCounter
from app.vision.types import Detection, Track

log = logging.getLogger("vision.engine")


@dataclass
class FrameResult:
    camera_id: int | None
    timestamp: float
    tracks: list[Track] = field(default_factory=list)
    behaviors: list[BehaviorHit] = field(default_factory=list)
    faces: list[dict] = field(default_factory=list)
    people: int = 0
    infer_ms: float = 0.0
    skipped: bool = False
    skip_reason: str = ""
    motion: float = 0.0


@dataclass
class CameraPipeline:
    """单路管线的全部可变状态。"""

    key: str
    camera_id: int | None = None
    tracker: IoUTracker = field(default_factory=IoUTracker)
    analyzer: BehaviorAnalyzer = field(default_factory=BehaviorAnalyzer)
    # 人数统计：中位数滤波 + 不对称迟滞，避免画面人数上下跳
    people_counter: PeopleCounter = field(default_factory=PeopleCounter)
    # 人脸识别的节流计数：识别（特征+检索）比检测贵一个量级，不能每帧都做
    face_tick: int = 0
    # 情绪平滑器：按轨迹做 EMA + 多数投票，纯内存、不落库
    emotion: EmotionSmoother = field(default_factory=EmotionSmoother)
    last_infer_at: float = 0.0
    frames: int = 0
    infer_ms_ema: float = 0.0
    # 最近一次推理的行为结果：跳过帧复用它，避免前端行为框闪烁
    last_behaviors: list[BehaviorHit] = field(default_factory=list)
    last_behaviors_at: float = 0.0
    # 最近一次推理的人脸框：同样在跳过帧复用，避免标注忽隐忽现
    last_faces: list[dict] = field(default_factory=list)
    last_faces_at: float = 0.0
    # 运动门控用的小尺寸灰度帧
    prev_gray: np.ndarray | None = None


def camera_pipeline_key(camera_id: int | None) -> str:
    return f"cam:{camera_id or 0}"


def video_pipeline_key(record_id: int) -> str:
    """视频文件分析使用独立管线，避免与实时链路互相 reset。"""
    return f"video:{record_id}"


# 渲染宽限：轨迹丢失后仍用卡尔曼预测框继续渲染的帧数。
# 检测偶发丢帧时框不会立刻消失（前端还有一层平滑跟随），避免"闪框"。
RENDER_GRACE_FRAMES = 2


def visible_tracks(pipe: CameraPipeline) -> list[Track]:
    """对外可见的轨迹：已通过确认门控，且不是长时间未观测的陈旧轨迹。"""
    return [tr for tr in pipe.tracker.tracks.values()
            if tr.confirmed and tr.misses <= RENDER_GRACE_FRAMES]


def raw_people(pipe: CameraPipeline) -> int:
    """本帧的原始人数：已确认且本帧确有检测的轨迹数。"""
    return len([tr for tr in pipe.tracker.tracks.values() if tr.confirmed and tr.misses == 0])


def _face_index():
    """延迟导入人脸索引。

    走函数内导入而不是模块顶层：``app.services`` 下的模块会反向引用 vision 包，
    顶层导入在将来重构时很容易演化成循环导入，这里保持单向依赖。
    """
    from app.services.face_index import face_index

    return face_index


class VisionEngine:
    """全局单例推理引擎。"""

    # 行为结果在跳过帧中的复用时长上限
    BEHAVIOR_CACHE_TTL = 1.5

    def __init__(self) -> None:
        self._pipelines: dict[str, CameraPipeline] = {}
        # 管线注册表自身的读写锁
        self._reg_lock = threading.RLock()
        # 每路管线一把互斥锁。infer() 由 asyncio.to_thread 在线程池里并发调用，
        # 而 CameraPipeline 内部（tracker.tracks、analyzer 的历史窗口、运动门控灰度帧）
        # 全是可变且非线程安全的状态：不串行化时，_cached() 遍历 tracker.tracks 与
        # 另一线程 pop 轨迹并发发生会抛 "dictionary changed size during iteration"，
        # 直接被上层捕获成"检测异常"，整路检测静默中断。
        self._pipe_locks: dict[str, threading.Lock] = {}

    # ---------- 模型 ----------
    def _primary(self):
        """主模型：优先姿态模型（一次推理同时得到人体框 + 关键点）。"""
        if settings.ENABLE_POSE and runtime_config.capability("pose"):
            try:
                return registry.pose_model(), "pose"
            except Exception as e:
                log.warning("姿态模型不可用，降级为纯检测模型：%s", e)
        return registry.detect_model(), "detect"

    def warmup(self) -> None:
        registry.warmup()

    # ---------- 管线 ----------
    def _pipe_lock(self, key: str) -> threading.Lock:
        with self._reg_lock:
            lock = self._pipe_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._pipe_locks[key] = lock
            return lock

    def pipeline(self, key: str, camera_id: int | None = None) -> CameraPipeline:
        with self._reg_lock:
            pipe = self._pipelines.get(key)
            if pipe is None:
                pipe = CameraPipeline(key=key, camera_id=camera_id)
                self._pipelines[key] = pipe
            elif camera_id is not None:
                pipe.camera_id = camera_id
            return pipe

    def reset(self, key: str) -> None:
        with self._reg_lock:
            self._pipelines.pop(key, None)

    def pipelines_status(self) -> dict:
        with self._reg_lock:
            pipes = list(self._pipelines.values())
        return {
            "count": len(pipes),
            "items": [
                {"key": p.key, "camera_id": p.camera_id, "frames": p.frames,
                 "infer_ms_ema": round(p.infer_ms_ema, 1),
                 "tracks": len(p.tracker.tracks)}
                for p in pipes
            ],
        }

    # ---------- 运动门控 ----------
    @staticmethod
    def _motion_score(pipe: CameraPipeline, frame: np.ndarray) -> float:
        """返回相邻帧灰度差的均值（0~255），用于判断画面是否有活动。"""
        try:
            small = cv2.resize(frame, (160, 120), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        except cv2.error:
            return 255.0  # 无法计算时不拦截推理
        prev = pipe.prev_gray
        pipe.prev_gray = gray
        if prev is None:
            return 255.0
        return float(cv2.absdiff(gray, prev).mean())

    # ---------- 推理 ----------
    def infer(
        self,
        frame: np.ndarray,
        camera_id: int | None = None,
        timestamp: float | None = None,
        force: bool = False,
        pipeline_key: str | None = None,
    ) -> FrameResult:
        """对单帧执行完整感知链路。

        ``force=True`` 时忽略限频与运动门控（用于视频文件分析与调试单帧）。

        同一路管线内部串行执行（见 ``_pipe_locks`` 的说明）；不同点位仍可并行。
        """
        key = pipeline_key or camera_pipeline_key(camera_id)
        with self._pipe_lock(key):
            return self._infer_locked(frame, camera_id, timestamp, force, key)

    def _infer_locked(
        self,
        frame: np.ndarray,
        camera_id: int | None,
        timestamp: float | None,
        force: bool,
        key: str,
    ) -> FrameResult:
        t = timestamp if timestamp is not None else time.time()
        pipe = self.pipeline(key, camera_id)

        # 运动强度：无论是否跳过推理都要回传，否则前端运行中恒显示"运动 0.00"
        motion = 0.0

        if not force:
            # 1) 限频：同一路多观看者共享该窗口
            min_interval = 1.0 / max(1, settings.INFER_FPS_LIMIT)
            if (t - pipe.last_infer_at) < min_interval:
                return self._cached(pipe, camera_id, t, "rate_limited")

            # 2) 运动门控：静止画面跳过推理，但至少每隔一段时间强制跑一帧
            if settings.MOTION_GATE_ENABLED:
                motion = self._motion_score(pipe, frame)
                idle_too_long = (t - pipe.last_infer_at) >= settings.MOTION_GATE_FORCE_INTERVAL_SEC
                if motion < settings.MOTION_DIFF_THRESHOLD and not idle_too_long:
                    return self._cached(pipe, camera_id, t, "no_motion", motion)

        pipe.last_infer_at = t
        pipe.frames += 1

        h, w = frame.shape[:2]
        started = time.perf_counter()

        detections: list[Detection] = []
        extra: list[Detection] = []

        # 人体检测关闭时不跑主模型：没有人员轨迹，行为判定自然也无从触发
        if runtime_config.capability("person"):
            model, kind = self._primary()
            try:
                res = model.predict(frame, **registry.predict_kwargs)[0]
                detections = self._parse(res, w, h, kind)
            except Exception as e:
                log.exception("主模型推理失败：%s", e)

        beh_model = registry.behavior_model()
        if beh_model is not None:
            try:
                bres = beh_model.predict(frame, **registry.predict_kwargs)[0]
                extra = [d for d in self._parse(bres, w, h, "behavior") if d.cls_name != "person"]
            except Exception as e:
                log.warning("行为模型推理失败：%s", e)

        tracks = pipe.tracker.update(detections, t)
        behaviors = pipe.analyzer.analyze(tracks, extra)

        faces: list[dict] = []
        if settings.ENABLE_FACE and runtime_config.capability("face"):
            try:
                faces = face_detector.detect(frame)
            except Exception as e:
                log.warning("人脸检测失败：%s", e)

        # 识别与情绪都挂在这一步之后：没有人脸框就没有对齐输入，也就没有特征
        self._process_faces(pipe, frame, faces, tracks)

        infer_ms = (time.perf_counter() - started) * 1000.0
        pipe.infer_ms_ema = infer_ms if pipe.frames == 1 else 0.85 * pipe.infer_ms_ema + 0.15 * infer_ms
        pipe.last_behaviors = behaviors
        pipe.last_behaviors_at = t
        pipe.last_faces = faces
        pipe.last_faces_at = t

        return FrameResult(
            camera_id=camera_id, timestamp=t,
            tracks=visible_tracks(pipe), behaviors=behaviors,
            faces=faces,
            people=pipe.people_counter.push(raw_people(pipe)),
            infer_ms=round(infer_ms, 1),
            motion=round(motion, 2),
        )

    def _process_faces(self, pipe: CameraPipeline, frame: np.ndarray,
                       faces: list[dict], tracks: list[Track]) -> None:
        """人脸识别 + 情绪识别 + 人脸↔人体轨迹绑定。

        三个设计要点：

        **① 节流。** 检测每帧都做（YuNet 约 5ms），但识别（SFace 对齐 + 128 维
        特征 + 检索）贵一个量级，因此按 ``FACE_RECOGNIZE_EVERY`` 节流。
        另按人脸框面积排序优先识别**大脸** —— 远处的脸本来就识别不准，
        把算力花在最近的人身上，有效召回反而更高。

        **② 轨迹级确认。** 单帧识别可能是错的，绝不能一命中就把姓名贴到人身上。
        这里用 ``Track.meta["pid_votes"]`` 做连续票确认，达到阈值才写入姓名；
        未达阈值时前端只显示"未识别"，宁可不显示也不能显示错的名字。

        **③ 失败关闭。** 识别模型缺失、索引为空、索引过期（刚发生过增删）时，
        一律不产生姓名，只保留检测框。功能降级，但不产生错误身份。
        """
        # raw 是 numpy 数组，必须在返回前剔除，否则 WebSocket JSON 序列化会直接失败
        try:
            enabled = (settings.ENABLE_FACE_ID and runtime_config.capability("face_id")
                       and face_id.ready and faces)
            if not enabled:
                return

            face_index = _face_index()
            if face_index.status().get("dirty"):
                return   # 索引过期期间不识别（fail-closed）

            pipe.face_tick += 1
            every = max(1, int(settings.FACE_RECOGNIZE_EVERY))
            if pipe.face_tick % every != 0:
                return

            emotion_on = (settings.ENABLE_EMOTION and runtime_config.capability("emotion")
                          and emotion_estimator.ready)
            emotion_every = max(1, int(settings.EMOTION_EVERY))
            want_emotion = emotion_on and (pipe.face_tick // every) % emotion_every == 0

            def area(f: dict) -> float:
                b = f["bbox"]
                return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

            ordered = sorted(faces, key=area, reverse=True)[: max(1, int(settings.FACE_RECOGNIZE_MAX))]
            alive: set[int] = set()
            for f in ordered:
                raw = f.get("raw")
                if raw is None:
                    continue
                aligned = face_id.align(frame, np.asarray(raw, dtype=np.float32))
                if aligned is None:
                    continue

                track = self._match_track(tracks, f["bbox"])
                if track is not None:
                    alive.add(track.tid)

                vec = face_id.feature_from_aligned(aligned)
                if vec is not None:
                    decision = face_index.identify(
                        vec, threshold=settings.FACE_MATCH_THRESHOLD,
                        margin=settings.FACE_MATCH_MARGIN)
                    if decision.get("matched") and track is not None:
                        self._vote_identity(track, decision)
                    f["match"] = {
                        "matched": bool(decision.get("matched")),
                        "reason": decision.get("reason", ""),
                        "score": decision.get("score"),
                    }

                if want_emotion:
                    probs = emotion_estimator.infer(aligned)
                    if probs is not None:
                        if track is not None:
                            res = pipe.emotion.update(track.tid, probs)
                            if res is not None:
                                track.meta["emotion"] = res.as_dict()
                        else:
                            # 未绑定到轨迹时无法做时序平滑，退化为单帧结果；
                            # 单帧情绪抖动大，因此只给置信度足够高的结果
                            from app.vision.emotion import EMOTIONS

                            idx = int(np.argmax(probs))
                            if float(probs[idx]) >= settings.EMOTION_CONF_MIN:
                                key, label, icon = EMOTIONS[idx]
                                f["emotion"] = {"key": key, "label": label, "icon": icon,
                                                "confidence": round(float(probs[idx]), 3)}

            # 把已确认的身份/情绪回填到人脸框，前端可直接在人脸上打名字
            for f in faces:
                track = self._match_track(tracks, f["bbox"])
                if track is None:
                    continue
                if "person" not in f and track.meta.get("person"):
                    f["person"] = track.meta["person"]
                if "emotion" not in f and track.meta.get("emotion"):
                    f["emotion"] = track.meta["emotion"]

            pipe.emotion.gc(alive or {t.tid for t in tracks})
        except Exception as e:  # noqa: BLE001 —— 识别失败绝不能中断检测与报警
            log.warning("人脸识别处理失败：%s", e)
        finally:
            for f in faces:
                f.pop("raw", None)

    @staticmethod
    def _match_track(tracks: list[Track], face_bbox: list[float]) -> Track | None:
        """把人脸框关联到人体轨迹：人脸中心落在人体框的**上半部**才算命中。

        只判"落在框内"是不够的 —— 前后两人重叠时，后排的人脸会落进前排的框里，
        于是姓名会贴错人。要求落点靠近人体框上部（头部应在的位置）能大幅减少这种串号，
        再用"离理想头部位置最近"来打破同框多人的歧义。
        """
        cx = (face_bbox[0] + face_bbox[2]) / 2
        cy = (face_bbox[1] + face_bbox[3]) / 2
        best: Track | None = None
        best_d = 1e9
        for tr in tracks:
            if tr.misses > 0:
                continue
            bx = tr.bbox
            w = max(1e-6, float(bx[2] - bx[0]))
            h = max(1e-6, float(bx[3] - bx[1]))
            # 允许一点外扩：人脸框有时会略超出人体框上沿
            if not (bx[0] - 0.02 * w <= cx <= bx[2] + 0.02 * w):
                continue
            if not (bx[1] - 0.08 * h <= cy <= bx[1] + 0.55 * h):
                continue
            ideal_y = bx[1] + 0.16 * h
            d = abs(cy - ideal_y)
            if d < best_d:
                best_d, best = d, tr
        return best

    @staticmethod
    def _vote_identity(track: Track, decision: dict) -> None:
        """轨迹级身份投票：连续 N 次一致才确认，避免单帧误识直接贴名字。"""
        summary = decision.get("summary") or {}
        if not summary:
            return
        key = f"{summary.get('owner_type')}:{summary.get('person_id')}"
        votes: dict[str, int] = track.meta.setdefault("pid_votes", {})
        votes[key] = votes.get(key, 0) + 1
        # 竞争者衰减：中途换了识别结果时，票数此消彼长，短时抖动无法累积成确认
        for k in list(votes):
            if k != key:
                votes[k] = max(0, votes[k] - 1)
        if votes[key] >= int(settings.FACE_CONFIRM_VOTES):
            track.meta["person"] = summary

    def _cached(self, pipe: CameraPipeline, camera_id: int | None, t: float,
                reason: str, motion: float = 0.0) -> FrameResult:
        """跳过推理时复用上次轨迹与行为结果，保证前端画面稳定。"""
        fresh = (t - pipe.last_behaviors_at) <= self.BEHAVIOR_CACHE_TTL
        faces_fresh = (t - pipe.last_faces_at) <= self.BEHAVIOR_CACHE_TTL
        return FrameResult(
            camera_id=camera_id, timestamp=t,
            tracks=visible_tracks(pipe),
            behaviors=list(pipe.last_behaviors) if fresh else [],
            faces=list(pipe.last_faces) if faces_fresh else [],
            people=pipe.people_counter.push(raw_people(pipe)),
            infer_ms=0.0, skipped=True, skip_reason=reason, motion=round(motion, 2),
        )

    # ---------- 结果解析 ----------
    @staticmethod
    def _parse(res, w: int, h: int, kind: str) -> list[Detection]:
        """把 ultralytics 的 Results 转成归一化 Detection 列表。"""
        out: list[Detection] = []
        boxes = getattr(res, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return out

        names = getattr(res, "names", {}) or {}
        xyxy = boxes.xyxy.detach().cpu().numpy()
        confs = boxes.conf.detach().cpu().numpy()
        clss = boxes.cls.detach().cpu().numpy().astype(int)

        kpts_np = None
        kconf_np = None
        kp = getattr(res, "keypoints", None)
        if kp is not None and getattr(kp, "xy", None) is not None and len(kp.xy) > 0:
            kpts_np = kp.xy.detach().cpu().numpy()                    # (N,17,2) 像素
            kc = getattr(kp, "conf", None)
            kconf_np = kc.detach().cpu().numpy() if kc is not None else None

        for i in range(len(xyxy)):
            name = str(names.get(int(clss[i]), clss[i]))
            bbox = np.array([xyxy[i][0] / w, xyxy[i][1] / h, xyxy[i][2] / w, xyxy[i][3] / h],
                            dtype=np.float64)
            bbox = np.clip(bbox, 0.0, 1.0)

            kpts = None
            if kpts_np is not None and i < len(kpts_np):
                k = np.zeros((kpts_np.shape[1], 3), dtype=np.float64)
                k[:, 0] = kpts_np[i][:, 0] / w
                k[:, 1] = kpts_np[i][:, 1] / h
                if kconf_np is not None and i < len(kconf_np):
                    k[:, 2] = kconf_np[i]
                else:
                    # 模型未输出逐点置信度时不能拿框置信度冒充，
                    # 否则无效关键点会被当作可信点，几何判定会失真。
                    k[:, 2] = 0.0
                k = np.clip(k, 0.0, 1.0)
                # ultralytics 对"不可见"的关键点输出 (0,0) 坐标，但置信度可能仍有 0.3~0.4。
                # 不归零的话，前端会从人身上画一条线飞到画面左上角（骨架"飞出去"）。
                degenerate = (k[:, 0] <= 0.0) & (k[:, 1] <= 0.0)
                k[degenerate, 2] = 0.0
                kpts = k

            if kind == "detect" and name != "person":
                continue  # 主检测模型只保留人
            out.append(Detection(bbox=bbox, conf=float(confs[i]), cls_name=name, kpts=kpts))
        return out


engine = VisionEngine()
