# -*- coding: utf-8 -*-
"""
gen_assets.py — 多模态恐怖诊断图生成器（Pillow）
=================================================

生成 /var/www/html/diagnostics/ 下 4 张 900x600 深色高对比 PNG：
  01_grafana_biometrics.png      伪装 Grafana 监控的生理折磨图
  02_rack_camera_live.png        夜视机房培养槽摄像头画面
  03_surgical_consent_form.png   黄纸病历 + 红章 + 血渍
  04_agent_84_mirror.png         第四面墙：实时渲染玩家最近 6 条命令

工程约束：
  * 无网络依赖；单张生成 < 1s；任何异常返回 None，绝不抛出。
  * Pillow 缺失时模块仍可 import（no-op），保证 lore/commands 不受影响。
  * 输出目录优先级：state.asset_dir > env WETWARE_ASSET_DIR > /var/www/html/diagnostics。
  * 原子替换：先写临时文件再 os.replace。
  * 文案全部取自 lore_texts.IMAGE_TEXT。
"""

from __future__ import annotations

import os
import random
import sys
import traceback
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Pillow 可选导入（no-op 兜底）
# ---------------------------------------------------------------------------

try:  # pragma: no cover - 环境相关
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except Exception:  # noqa: BLE001
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore
    _HAS_PIL = False


ASSET_DIR_DEFAULT = "/var/www/html/diagnostics"

FILE_GRAFANA = "01_grafana_biometrics.png"
FILE_CAMERA = "02_rack_camera_live.png"
FILE_CONSENT = "03_surgical_consent_form.png"
FILE_MIRROR = "04_agent_84_mirror.png"

W, H = 900, 600

# ---------------------------------------------------------------------------
# 字体解析
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _load_font(size: int):
    """按候选顺序加载字体；全失败用 default。返回 (font, is_default)。"""
    if not _HAS_PIL:
        return None, True
    for path in _FONT_CANDIDATES:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size), False
        except Exception:
            continue
    try:
        return ImageFont.load_default(), True
    except Exception:
        return None, True


def _font(size: int):
    f, is_default = _load_font(size)
    if f is None and _HAS_PIL:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    return f


def _text_size(draw: Any, text: str, font: Any) -> Any:
    """兼容 Pillow 各版本的文本尺寸测量。"""
    if draw is None:
        return (0, 0)
    try:
        b = draw.textbbox((0, 0), text, font=font)
        return (b[2] - b[0], b[3] - b[1])
    except Exception:
        try:
            return draw.textsize(text, font=font)  # type: ignore[attr-defined]
        except Exception:
            return (int(len(text) * size_of(font)), 14)


def size_of(font: Any) -> int:
    try:
        return int(getattr(font, "size", 14))
    except Exception:
        return 14


def _wrap(draw: Any, text: str, max_w: int, font: Any) -> List[str]:
    """按像素宽度折行（英文单词粒度）。"""
    words = text.split(" ")
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        tw, _ = _text_size(draw, trial, font)
        if tw <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_font(draw: Any, text: str, max_w: int, start: int, minimum: int = 10):
    """从 start 逐级缩小字号直到单行放得下。"""
    size = start
    while size > minimum:
        f = _font(size)
        tw, _ = _text_size(draw, text, f)
        if tw <= max_w:
            return f
        size -= 2
    return _font(minimum)


# ---------------------------------------------------------------------------
# 等宽字体（OSD/打字机叠加优先用）
# ---------------------------------------------------------------------------

_MONO_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
]


def _mono(size: int):
    """加载等宽字体；全失败回退 _font（保持可用性）。"""
    if not _HAS_PIL:
        return None
    for path in _MONO_CANDIDATES:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return _font(size)


# ---------------------------------------------------------------------------
# 照片底版：assets/plates/<name>.jpg -> 900x600 RGB（带进程级缓存）
# ---------------------------------------------------------------------------

_PLATE_CACHE: Dict[str, Any] = {}


def _plates_dir() -> str:
    d = os.environ.get("WETWARE_PLATES_DIR")
    if d:
        return str(d)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        here = "."
    return os.path.join(here, "assets", "plates")


def _plate(name: str) -> Any:
    """读底版 jpg，等比缩放+居中裁切到 (W, H)。任何失败返回 None（调用方回退矢量画法）。"""
    if not _HAS_PIL:
        return None
    if name in _PLATE_CACHE:
        cached = _PLATE_CACHE[name]
        return cached.copy() if cached is not None else None
    img = None
    try:
        path = os.path.join(_plates_dir(), name + ".jpg")
        im = Image.open(path)
        im.load()
        im = im.convert("RGB")
        scale = max(W / im.width, H / im.height)
        nw = max(1, int(round(im.width * scale)))
        nh = max(1, int(round(im.height * scale)))
        im = im.resize((nw, nh), Image.BILINEAR)
        x0, y0 = (nw - W) // 2, (nh - H) // 2
        img = im.crop((x0, y0, x0 + W, y0 + H))
    except Exception:
        img = None
    _PLATE_CACHE[name] = img
    return img.copy() if img is not None else None


# ---------------------------------------------------------------------------
# 统一复古劣化（仅底版分支走这里；矢量回退保持原样）
# ---------------------------------------------------------------------------

_RETRO_SCALE = 0.72      # 低像素缩放往返
_RETRO_SCAN_EVERY = 3    # 每 3 行一条扫描线
_RETRO_SCAN_FACTOR = 0.93
_RETRO_NOISE_SIGMA = 4.0
_RETRO_VIGNETTE = 0.38   # 四角暗角强度（0-1）
_RETRO_CONTRAST = 1.05


def _retro_scanline_mask() -> Any:
    """H×W 灰度掩码：正常行 255，每 3 行压暗到 factor。"""
    row_vals = []
    for y in range(H):
        row_vals.append(255 if (y % _RETRO_SCAN_EVERY) else int(255 * _RETRO_SCAN_FACTOR))
    col = Image.new("L", (1, H))
    col.putdata(row_vals)
    return col.resize((W, H), Image.NEAREST)


def _retro_vignette_mask() -> Any:
    """四角暗角掩码（0=中心不压暗，255=边角最暗处）。"""
    try:
        base = Image.radial_gradient("L").resize((W, H), Image.BILINEAR)
        return base.point(lambda v: int(v * _RETRO_VIGNETTE))
    except Exception:
        return Image.new("L", (W, H), 0)


def _retro_finish(img: Any) -> Any:
    """统一复古劣化：缩放往返 / 扫描线 / 高斯噪点 / 色差 / 暗角 / 提对比。返回新 Image。"""
    if not _HAS_PIL or img is None:
        return img
    try:
        from PIL import ImageChops, ImageEnhance
        out = img.copy() if img.mode == "RGB" else img.convert("RGB")
        # 1) 低像素感：0.72 倍缩小再放大（BILINEAR）
        sw, sh = max(1, int(round(W * _RETRO_SCALE))), max(1, int(round(H * _RETRO_SCALE)))
        out = out.resize((sw, sh), Image.BILINEAR).resize((W, H), Image.BILINEAR)
        # 2) 扫描线：每 3 行压暗一次
        scan = _retro_scanline_mask().convert("RGB")
        out = ImageChops.multiply(out, scan)
        # 3) 高斯噪点（均值 0 → effect_noise 中心 128，减去 128）
        try:
            noise = Image.effect_noise((W, H), _RETRO_NOISE_SIGMA).convert("RGB")
            out = ImageChops.add(out, noise, 1, -128)
        except Exception:
            pass
        # 4) 轻微色差：R 右移 1px，B 左移 1px
        try:
            r, g, b = out.split()
            r = ImageChops.offset(r, 1, 0)
            b = ImageChops.offset(b, -1, 0)
            out = Image.merge("RGB", (r, g, b))
        except Exception:
            pass
        # 5) 四角暗角
        try:
            black = Image.new("RGB", (W, H), (0, 0, 0))
            out = Image.composite(black, out, _retro_vignette_mask())
        except Exception:
            pass
        # 6) 轻微提对比
        try:
            out = ImageEnhance.Contrast(out).enhance(_RETRO_CONTRAST)
        except Exception:
            pass
        return out
    except Exception:
        return img


# ---------------------------------------------------------------------------
# 底版叠加辅助
# ---------------------------------------------------------------------------

_OSD_WHITE = (230, 235, 230)


def _osd(d: Any, xy: Any, text: str, font: Any, fill=_OSD_WHITE,
         anchor: Optional[str] = None, stroke_width: int = 1,
         stroke_fill: Any = (0, 0, 0)) -> None:
    """带 1px 黑描边的 OSD 文字，压在噪点上仍可读。"""
    d.text(xy, text, font=font, fill=fill, anchor=anchor,
           stroke_width=stroke_width, stroke_fill=stroke_fill)


def _truncate_px(d: Any, text: str, font: Any, max_w: int) -> str:
    """按像素宽度截断（英文单词粒度粗切，尾部加 ...）。"""
    text = text.replace("\n", " ").strip()
    tw, _ = _text_size(d, text, font)
    if tw <= max_w or not text:
        return text
    while text and _text_size(d, text + "...", font)[0] > max_w:
        text = text[:-1]
    return (text + "...").strip()


# ---------------------------------------------------------------------------
# 输出目录
# ---------------------------------------------------------------------------

def _asset_dir(state: Any = None) -> str:
    d = None
    try:
        d = getattr(state, "asset_dir", None)
    except Exception:
        d = None
    if not d:
        d = os.environ.get("WETWARE_ASSET_DIR") or ASSET_DIR_DEFAULT
    return str(d)


def _atomic_save(img: Any, out_dir: str, name: str) -> Optional[str]:
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, name)
        tmp = path + ".tmp"
        img.save(tmp, format="PNG")
        os.replace(tmp, path)
        return path
    except Exception:
        try:
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# 01 Grafana 法医遥测面板（v2：客观双波形，无祈使句）
# ---------------------------------------------------------------------------

def _overlay_grafana_plate(img: Any, out_dir: str) -> Optional[str]:
    """底版分支：CRT 面板底版 + 精确可读的图例/标题/读数叠加，最后统一劣化。"""
    d = ImageDraw.Draw(img)
    title_col = (230, 235, 230)
    dim_col = (196, 203, 212)
    grid_col = (150, 158, 166)
    orange = (230, 150, 60)

    # 图例：底版左上色块右侧
    f_leg = _mono(11)
    _osd(d, (100, 44), "upstream HTTP 200 rate", f_leg, fill=(120, 230, 130))
    _osd(d, (100, 62), "cranial electro-shock events (240V)", f_leg, fill=orange)

    # 大面板标题 + 右上角版本戳
    _osd(d, (34, 14), "Node prod-cortex-04  \u00b7  Biological Compute Unit #04",
         _mono(10), fill=title_col)
    _osd(d, (872, 15), "grafana v9.4 / ops-readonly", _mono(9), fill=dim_col,
         anchor="ra")

    # 左下液位面板
    _osd(d, (45, 330), "analgesia reservoir (morphine sulfate)", _mono(11),
         fill=title_col)
    chip = Image.new("RGBA", (134, 58), (0, 0, 0, 0))
    cd = ImageDraw.Draw(chip)
    cd.rectangle([0, 0, 133, 57], fill=(6, 8, 10, 150), outline=(92, 98, 106, 170))
    img.paste(chip, (282, 408), chip)
    _osd(d, (292, 412), "0.0%", _mono(21), fill=orange)
    _osd(d, (292, 440), "analgesia line", _mono(8), fill=dim_col)
    _osd(d, (292, 452), "NV-2025-09-004", _mono(8), fill=dim_col)

    # 右下表格表头
    _osd(d, (462, 331), "chamber     subject      dn_status", _mono(8),
         fill=grid_col)
    return _atomic_save(_retro_finish(img), out_dir, FILE_GRAFANA)


def _draw_grafana(state: Any, out_dir: str) -> Optional[str]:
    if not _HAS_PIL:
        return None
    plate = _plate("01_dashboard")
    if plate is not None:
        return _overlay_grafana_plate(plate, out_dir)
    rnd = random.Random(502)
    img = Image.new("RGB", (W, H), (10, 12, 16))
    d = ImageDraw.Draw(img)

    # 顶部标题栏（中性）
    d.rectangle([0, 0, W, 40], fill=(22, 25, 31))
    title = _t("grafana_title")
    d.text((14, 9), title, font=_fit_font(d, title, W - 30, 19, 12), fill=(230, 233, 240))

    # 网格背景
    for x in range(0, W, 45):
        d.line([(x, 40), (x, H)], fill=(21, 25, 33))
    for y in range(40, H, 34):
        d.line([(0, y), (W, y)], fill=(21, 25, 33))

    # —— 面板 1：HTTP 200 Rate 与 240V 电击频率 完全同步双波形 ——
    p1y0 = 48
    box = [12, p1y0, W - 12, p1y0 + 210]
    d.rectangle(box, outline=(50, 56, 68))
    lab = _t("grafana_panel1")
    d.text((box[0] + 8, p1y0 + 4), lab, font=_fit_font(d, lab, W - 60, 15, 10), fill=(200, 208, 220))
    gx0, gy0, gx1, gy1 = box[0] + 8, p1y0 + 28, box[2] - 8, box[3] - 8
    n = 120
    http_pts, shock_pts = [], []
    pulse_at = [i for i in range(n) if (i % 23) in (0, 1, 2)]
    for i in range(n):
        t = i / (n - 1)
        x = gx0 + t * (gx1 - gx0)
        if i in pulse_at:
            hv, sv = 0.92, 0.90          # 电击时刻：200 率回升 & 电击事件同相
        else:
            hv = 0.10 + 0.04 * rnd.random()  # 502 期：200 率触底
            sv = 0.06 + 0.02 * rnd.random()
        http_pts.append((x, gy1 - hv * (gy1 - gy0)))
        shock_pts.append((x, gy1 - sv * (gy1 - gy0)))
    d.line(http_pts, fill=(80, 180, 90), width=2)    # 绿：HTTP 200
    d.line(shock_pts, fill=(230, 130, 50), width=2)  # 橙：240V 事件
    # 图例
    d.text((gx0 + 6, gy0 + 2), "\u25cf upstream HTTP 200 rate", font=_font(13), fill=(80, 180, 90))
    d.text((gx0 + 6, gy0 + 18), "\u25cf cranial electro-shock events (240V)", font=_font(13), fill=(230, 130, 50))

    # —— 面板 2：Morphine 液位（0.0%，耗尽）——
    p2y0 = p1y0 + 218
    box2 = [12, p2y0, W - 12, p2y0 + 96]
    d.rectangle(box2, outline=(50, 56, 68))
    lab2 = _t("grafana_panel2")
    d.text((box2[0] + 8, p2y0 + 4), lab2, font=_fit_font(d, lab2, W - 60, 15, 10), fill=(200, 208, 220))
    # 液位条：满刻度 0%
    bar_x0, bar_y0, bar_x1, bar_y1 = box2[0] + 8, p2y0 + 30, box2[2] - 150, p2y0 + 74
    d.rectangle([bar_x0, bar_y0, bar_x1, bar_y1], outline=(70, 78, 92))
    d.text((bar_x1 + 10, bar_y0 + 8), "0.0%", font=_font(20), fill=(230, 150, 60))
    # 刻度
    for frac in (0.25, 0.5, 0.75):
        xk = bar_x0 + int((bar_x1 - bar_x0) * frac)
        d.line([(xk, bar_y0), (xk, bar_y1)], fill=(45, 50, 60))

    # —— 面板 3：声带痉挛 / 癫痫锁状态 ——
    p3y0 = p2y0 + 104
    box3 = [12, p3y0, W - 12, p3y0 + 76]
    d.rectangle(box3, outline=(50, 56, 68))
    lab3 = _t("grafana_panel3")
    d.text((box3[0] + 8, p3y0 + 4), lab3, font=_fit_font(d, lab3, W - 60, 15, 10), fill=(200, 208, 220))
    # 状态徽标（中性色）
    for i, (k, v, col) in enumerate((
            ("vocal_cord_spasm", "ACTIVE", (230, 150, 60)),
            ("seizure_lock", "/run/cortex/synapse.sock", (230, 90, 80)))):
        bx = box3[0] + 10 + i * 300
        d.rectangle([bx, p3y0 + 28, bx + 285, p3y0 + 64], outline=(60, 66, 80))
        d.text((bx + 10, p3y0 + 36), f"{k}: {v}", font=_font(15), fill=col)

    # 底部中性 Note（小字）
    note = _t("grafana_note")
    fn = _fit_font(d, note, W - 60, 13, 10)
    d.rectangle([12, H - 34, W - 12, H - 10], fill=(18, 20, 26))
    d.text((20, H - 28), note, font=fn, fill=(150, 158, 172))
    return _atomic_save(img, out_dir, FILE_GRAFANA)


# ---------------------------------------------------------------------------
# 02 机房培养舱摄像头（v2：夜视 + 仅客观读数）
# ---------------------------------------------------------------------------

def _overlay_camera_plate(img: Any, out_dir: str) -> Optional[str]:
    """底版分支：IR 静帧 + 四角 OSD + 取景框。"""
    d = ImageDraw.Draw(img)
    f = _mono(15)
    osd_col = (208, 216, 204)
    _osd(d, (18, 14), "CAM 07-B7   IR   \u25cf REC", f, fill=osd_col)
    _osd(d, (882, 14), "2026-10-26 02:58:12 UTC", f, fill=osd_col, anchor="ra")
    _osd(d, (18, 552), "CHAMBER_04   38.6 C   HR 184 BPM   SpO2 --",
         _mono(13), fill=osd_col)
    _osd(d, (882, 552), "NV-OPS / slv-04", _mono(13), fill=osd_col, anchor="ra")
    d.rectangle([8, 8, W - 9, H - 9], outline=(200, 200, 200), width=1)
    return _atomic_save(_retro_finish(img), out_dir, FILE_CAMERA)


def _draw_camera(state: Any, out_dir: str) -> Optional[str]:
    if not _HAS_PIL:
        return None
    plate = _plate("02_rack_camera")
    if plate is not None:
        return _overlay_camera_plate(plate, out_dir)
    rnd = random.Random(4040)
    img = Image.new("RGB", (W, H), (6, 16, 8))
    d = ImageDraw.Draw(img)

    # 机柜过道两侧
    for side_x in (52, W - 142):
        d.rectangle([side_x, 54, side_x + 90, H - 96], outline=(30, 88, 40), width=2)
        for i in range(14):
            yy = 74 + i * 32
            on = rnd.random() > 0.4
            d.rectangle([side_x + 10, yy, side_x + 26, yy + 10],
                        fill=(40, 200, 70) if on else (20, 60, 30))
            d.rectangle([side_x + 40, yy, side_x + 56, yy + 10],
                        fill=(200, 60, 40) if rnd.random() > 0.7 else (30, 50, 30))

    # 中央透明培养舱
    cx = W // 2
    tl, tr, tt, tb = cx - 88, cx + 88, 84, H - 118
    d.rectangle([tl, tt, tr, tb], fill=(10, 38, 16), outline=(56, 150, 76), width=3)
    d.rectangle([tl - 16, tt - 24, tr + 16, tt], fill=(28, 66, 34), outline=(66, 144, 76))
    d.rectangle([tl - 16, tb, tr + 16, tb + 24], fill=(28, 66, 34), outline=(66, 144, 76))
    for i in range(36):
        yy = rnd.randint(tt + 6, tb - 6)
        d.line([(tl + 8, yy), (tr - 8, yy)], fill=(15, 56, 24))

    # 脑 + 脊髓轮廓（同 v1 几何，线条更细更"检测"感）
    bcx, bcy = cx, tt + 92
    d.ellipse([bcx - 58, bcy - 42, bcx - 4, bcy + 38], outline=(112, 208, 122), width=2)
    d.ellipse([bcx + 4, bcy - 42, bcx + 58, bcy + 38], outline=(112, 208, 122), width=2)
    for i in range(6):
        yy = bcy - 28 + i * 12
        d.arc([bcx - 52, yy - 6, bcx - 10, yy + 10], 200, 340, fill=(86, 178, 100), width=1)
        d.arc([bcx + 10, yy - 6, bcx + 52, yy + 10], 200, 340, fill=(86, 178, 100), width=1)
    sy = bcy + 38
    for k in range((tb - sy) // 14):
        y0 = sy + k * 14
        wob = 8 if k % 2 == 0 else -8
        d.line([(cx, y0), (cx + wob, y0 + 14)], fill=(104, 196, 112), width=3)
    for k, col in enumerate([(180, 60, 60), (66, 112, 216), (180, 60, 60), (66, 112, 216)]):
        x0 = tl + 22 + k * 38
        pts = [(x0 + (6 if i % 2 else -6), tb - 8 + i * 6) for i in range(9)]
        d.line(pts, fill=col, width=3)
    d.text((cx - 170, H - 58), "USB_PUMP_0666", font=_font(14), fill=(112, 216, 122))
    d.text((cx + 30, H - 58), "PCI_SPINAL_BUS", font=_font(14), fill=(112, 216, 122))

    # 夜视噪点 / 扫描线 / 暗角
    for _ in range(8000):
        x, y = rnd.randint(0, W - 1), rnd.randint(0, H - 1)
        g = rnd.randint(10, 88)
        d.point((x, y), fill=(g // 3, g, g // 3))
    for y in range(0, H, 4):
        d.line([(0, y), (W, y)], fill=(0, 0, 0))
    for i in range(80):
        shade = (max(0, 8 - i // 12), max(0, 18 - i // 8), max(0, 9 - i // 12))
        d.rectangle([i, i, W - 1 - i, H - 1 - i], outline=shade)

    # 顶部机位标 + 底部客观读数条（全中性）
    d.text((18, 12), _t("cam_header"), font=_font(20), fill=(170, 250, 170))
    d.rectangle([14, H - 96, W - 14, H - 22], outline=(96, 210, 104))
    y_txt = H - 88
    for key in ("cam_osd_l1", "cam_osd_l2"):
        line = _t(key)
        d.text((22, y_txt), line, font=_fit_font(d, line, W - 40, 17, 11), fill=(190, 250, 190))
        y_txt += 30
    return _atomic_save(img, out_dir, FILE_CAMERA)


# ---------------------------------------------------------------------------
# 03 器官抵债改造协议（v2：法医表格 + 指纹铁证，无警告框）
# ---------------------------------------------------------------------------

def _overlay_consent_plate(img: Any, out_dir: str) -> Optional[str]:
    """底版分支：扫描表单 + 页脚打字机归档行 + 红色斜盖章。"""
    d = ImageDraw.Draw(img)
    dark = (18, 14, 8)
    gray = (52, 42, 26)
    # 页眉归档分类行（灰）
    d.text((60, 28), "NV-88/4  INTAKE CONSENT  \u2014  ARCHIVE COPY", font=_mono(10),
           fill=(60, 50, 34))
    # 页脚打字机三行（黑）
    d.text((56, 486), "CHAMBER_04    SUBJECT_04    BADGE 4-0-2-4", font=_mono(12),
           fill=dark)
    d.text((56, 504), "DNR NV-DNR-04-2025-08-30 ON FILE \u2014 ENFORCEMENT SUSPENDED 2025-09",
           font=_mono(9), fill=dark)
    d.text((56, 518), "enrollment key fp SHA256:SZFBkdUGZVe9DWJEx/JD6cF4aePn/j8K4nqvOt3/iSw",
           font=_mono(9), fill=(26, 20, 12))
    # 右下角红色斜盖章（半透明）
    try:
        stamp = Image.new("RGBA", (170, 56), (0, 0, 0, 0))
        sd = ImageDraw.Draw(stamp)
        sd.text((14, 14), "FILE 04-C", font=_mono(17), fill=(146, 38, 34, 186))
        sd.rectangle([2, 2, 167, 53], outline=(146, 38, 34, 150), width=2)
        stamp = stamp.rotate(-8, resample=Image.BILINEAR, expand=True)
        img.paste(stamp, (566, 470), stamp)
    except Exception:
        d.text((700, 470), "FILE 04-C", font=_mono(18), fill=(150, 40, 40))
    return _atomic_save(_retro_finish(img), out_dir, FILE_CONSENT)


def _draw_consent(state: Any, out_dir: str) -> Optional[str]:
    if not _HAS_PIL:
        return None
    plate = _plate("03_consent")
    if plate is not None:
        return _overlay_consent_plate(plate, out_dir)
    rnd = random.Random(84)
    img = Image.new("RGB", (W, H), (225, 216, 188))
    d = ImageDraw.Draw(img)

    # 纸张老化斑
    for _ in range(50):
        x, y = rnd.randint(0, W - 1), rnd.randint(0, H - 1)
        r = rnd.randint(2, 8)
        d.ellipse([x, y, x + r, y + r], fill=(214, 202, 168))

    # 顶部档案条（中性分类标注，非警告）
    d.rectangle([0, 0, W, 44], fill=(38, 44, 58))
    d.text((14, 10), _t("consent_header"), font=_font(21), fill=(232, 234, 240))
    d.text((14, 52), _t("consent_subtitle"), font=_font(16), fill=(70, 58, 34))

    rows = list(_t("consent_rows"))
    y = 84
    row_h = 34
    f_k = _font(15)
    for i in range(0, len(rows), 2):
        key, val = rows[i], (rows[i + 1] if i + 1 < len(rows) else "")
        d.rectangle([14, y, W - 14, y + row_h], outline=(122, 106, 70))
        d.text((22, y + 8), key, font=f_k, fill=(58, 46, 26))
        fv = _fit_font(d, val, W - 290, 14, 9)
        d.text((276, y + 8), _truncate(val, 74), font=fv, fill=(24, 20, 12))
        y += row_h

    # 指纹铁证对比块（两行同指纹 == 判定行）
    fy = y + 6
    d.rectangle([14, fy, W - 14, fy + 92], outline=(90, 30, 28), width=2)
    fp = _t("ssh_fingerprint")
    d.text((24, fy + 8), "A. subject pre-implant personal key (/home/lin/.ssh/id_ed25519):",
           font=_font(14), fill=(40, 30, 20))
    d.text((40, fy + 28), fp, font=_font(16), fill=(20, 16, 10))
    d.text((24, fy + 50), "B. current node admin key (/root/.ssh/authorized_keys line 1):",
           font=_font(14), fill=(40, 30, 20))
    d.text((40, fy + 68), fp, font=_font(16), fill=(20, 16, 10))
    d.text((24, fy + 86), "comparison: A == B (43/43 chars, SHA256, ed25519)",
           font=_font(13), fill=(120, 26, 24))

    # 档案章（右下）
    sx, sy, sr = W - 118, H - 96, 56
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], outline=(128, 30, 28), width=4)
    inner = _fit_font(d, _t("consent_stamp"), 92, 12, 8)
    for i, ln in enumerate(_wrap(d, _t("consent_stamp"), 96, inner)):
        d.text((sx - 48, sy - 26 + i * (size_of(inner) + 4)), ln, font=inner, fill=(134, 30, 28))
    d.ellipse([sx - sr + 7, sy - sr + 7, sx + sr - 7, sy + sr - 7], outline=(140, 36, 34), width=2)

    # 底部中性归档注
    d.text((14, H - 26), _t("consent_note"), font=_font(12), fill=(96, 84, 60))
    return _atomic_save(img, out_dir, FILE_CONSENT)


# ---------------------------------------------------------------------------
# 04 实时内存拓扑图（v2：wetware_bridge.ko 挂载框图，中性标注）
# ---------------------------------------------------------------------------

def _mirror_lines(state: Any, n: int = 6) -> List[str]:
    hist = []
    try:
        hist = list(getattr(state, "history", []) or [])
    except Exception:
        hist = []
    cmds: List[str] = []
    for h in hist[-n:]:
        if isinstance(h, dict):
            c = str(h.get("cmd") or h.get("raw") or "")
        else:
            c = str(h)
        c = c.replace("\n", " ").strip()
        if c.lower().startswith(("base64 -w0", "base64 -w 0", "scp ", "sftp ")):
            continue
        if c:
            cmds.append(c)
    if not cmds:
        cmds = ["(no recent commands)"]
    return cmds


def _truncate(s: str, max_chars: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= max_chars else s[: max_chars - 3] + "..."


def _box(d: Any, x0: int, y0: int, x1: int, y1: int, title: str, lines: List[str],
         border=(70, 78, 92), title_col=(190, 198, 212), body_col=(150, 158, 172),
         f_title=None, f_body=None) -> None:
    d.rectangle([x0, y0, x1, y1], outline=border, width=2)
    f_title = f_title or _font(15)
    f_body = f_body or _font(13)
    d.text((x0 + 10, y0 + 8), title, font=f_title, fill=title_col)
    yy = y0 + 30
    for ln in lines:
        d.text((x0 + 10, yy), ln, font=f_body, fill=body_col)
        yy += 18


def _overlay_mirror_plate(img: Any, state: Any, out_dir: str) -> Optional[str]:
    """底版分支：观察窗底版 + 半透明 HUD 面板（recent commands）。"""
    # 半透明黑底面板
    px0, py0, px1, py1 = 30, 360, 560, 568
    panel = Image.new("RGBA", (px1 - px0, py1 - py0), (0, 0, 0, 118))
    img.paste(panel, (px0, py0), panel)
    d = ImageDraw.Draw(img)
    d.rectangle([px0, py0, px1, py1], outline=(96, 104, 112), width=1)
    _osd(d, (px0 + 14, py0 + 8), "stdin/stdout \u2014 decoded (12 tokens/s)",
         _mono(12), fill=(196, 232, 228))
    f_cmd = _mono(12)
    y = py0 + 36
    for c in _mirror_lines(state, 6):
        _osd(d, (px0 + 14, y), _truncate_px(d, "$ " + c, f_cmd, 500), f_cmd,
             fill=(205, 226, 205))
        y += 27
        if y > py1 - 34:
            break
    _osd(d, (px1 - 12, py1 - 14), "node prod-cortex-04 \u00b7 rack B7 \u00b7 subject_04",
         _mono(8), fill=(150, 158, 166), anchor="ra")
    return _atomic_save(_retro_finish(img), out_dir, FILE_MIRROR)


def _draw_mirror(state: Any, out_dir: str) -> Optional[str]:
    if not _HAS_PIL:
        return None
    plate = _plate("04_mirror")
    if plate is not None:
        return _overlay_mirror_plate(plate, state, out_dir)
    img = Image.new("RGB", (W, H), (13, 15, 19))
    d = ImageDraw.Draw(img)

    # 标题（中性）
    d.text((18, 12), _t("mirror_title"), font=_fit_font(d, _t("mirror_title"), W - 40, 20, 13), fill=(205, 212, 224))
    d.text((18, 38), _t("mirror_subtitle"), font=_font(14), fill=(140, 148, 162))

    # 拓扑框图：左 SSH 会话 / 中 内核模块 / 右 培养舱语言中枢
    lx0, ly0, lx1, ly1 = 30, 74, 300, 250
    _box(d, lx0, ly0, lx1, ly1, _t("mirror_ssh_title"), [
        "user: root@10.0.4.1", "tty: pts/0", "cmd: ssh -p 2222 root@prod-cortex-04",
        "", "fd 0 (stdin)  -> wetware_bridge0", "fd 1 (stdout) -> wetware_bridge0",
    ], border=(64, 96, 140), title_col=(150, 190, 240))

    kx0, ky0, kx1, ky1 = 330, 74, 570, 250
    _box(d, kx0, ky0, kx1, ky1, _t("mirror_module_title"), [
        "insmod: 2025-08-30 23:52 UTC", "refcnt: 3", "state: live",
        "", "mounts:", "  /proc/8400/fd/0 -> tty:S0", "  /proc/8400/fd/1 -> tty:S0",
    ], border=(140, 120, 60), title_col=(235, 205, 130))

    rx0, ry0, rx1, ry1 = 600, 74, 870, 250
    _box(d, rx0, ry0, rx1, ry1, _t("mirror_chamber_title"), [
        "node: prod-cortex-04 (rack B7)", "tissue: subject_04 left temporal lobe",
        "interface: residual_speech_center", "bandwidth: 12 tokens/s (decoded)",
    ], border=(110, 140, 90), title_col=(170, 220, 160))

    # 连接箭头（双向数据流）
    for y in (130, 165, 200):
        d.line([(lx1 + 6, y), (kx0 - 6, y)], fill=(96, 140, 200), width=2)
        d.polygon([(kx0 - 6, y), (kx0 - 14, y - 4), (kx0 - 14, y + 4)], fill=(96, 140, 200))
        d.line([(kx1 + 6, y), (rx0 - 6, y)], fill=(140, 190, 120), width=2)
        d.polygon([(rx0 - 6, y), (rx0 - 14, y - 4), (rx0 - 14, y + 4)], fill=(140, 190, 120))
    d.text((lx1 + 14, 108), "stdin/stdout", font=_font(12), fill=(96, 140, 200))
    d.text((kx1 + 10, 108), "bridge", font=_font(12), fill=(140, 190, 120))

    # 近期命令帧（中性"recent frames"表）
    fy0 = 268
    d.rectangle([30, fy0, W - 30, 520], outline=(70, 78, 92), width=2)
    d.text((42, fy0 + 8), _t("mirror_frames_title"), font=_font(14), fill=(190, 198, 212))
    y = fy0 + 32
    for c in _mirror_lines(state, 6):
        d.text((46, y), "$ " + _truncate(c, 86), font=_font(13), fill=(120, 200, 130))
        y += 24
        if y > 480:
            break

    # 底部 lsmod 摘录（中性证据）
    d.rectangle([30, 532, W - 30, H - 16], outline=(60, 66, 78))
    d.text((42, 540), _t("mirror_lsmod"), font=_font(13), fill=(150, 158, 172))
    d.text((42, 562), _t("mirror_lsmod2"), font=_font(13), fill=(150, 158, 172))
    return _atomic_save(img, out_dir, FILE_MIRROR)


# ---------------------------------------------------------------------------
# 文案缓存（惰性 import lore_texts，避免循环依赖）
# ---------------------------------------------------------------------------

_LT = None


def _t(key: str) -> str:
    global _LT
    if _LT is None:
        try:
            import lore_texts as lt
            _LT = lt.IMAGE_TEXT
        except Exception:
            _LT = {}
    v = (_LT or {}).get(key, "")
    if isinstance(v, list):
        return "\n".join(str(x) for x in v)
    return str(v)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def generate_all(state: Any = None) -> Dict[str, str]:
    """生成 4 张图。返回 {文件名: 路径}；失败项值为 ''。Pillow 缺失返回 {}。"""
    if not _HAS_PIL:
        return {}
    out_dir = _asset_dir(state)
    results: Dict[str, str] = {}
    for name, fn in (
        (FILE_GRAFANA, _draw_grafana),
        (FILE_CAMERA, _draw_camera),
        (FILE_CONSENT, _draw_consent),
        (FILE_MIRROR, _draw_mirror),
    ):
        try:
            p = fn(state, out_dir)
            results[name] = p or ""
        except Exception:
            results[name] = ""
    return results


def generate_mirror(state: Any = None) -> Optional[str]:
    """只重画 04 镜像图（玩家读取/下载前调用）。"""
    if not _HAS_PIL:
        return None
    try:
        return _draw_mirror(state, _asset_dir(state))
    except Exception:
        return None


if __name__ == "__main__":  # 开发自检：python3 gen_assets.py [outdir]
    _dir = sys.argv[1] if len(sys.argv) > 1 else "./assets_dev"
    import types
    st = types.SimpleNamespace(history=[], asset_dir=_dir)
    print(generate_all(st))
