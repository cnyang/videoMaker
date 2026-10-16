"""拍點偵測：用 librosa（ffprobe 偵測不了節拍），以實際拍點決定每鏡切點。"""

import librosa
import numpy as np

BEATS_PER_SHOT = 8  # 4/4、2 小節
HOP = 128
SILENCE_DB = -45.0


def analyze(path, bpm: int, n_shots: int) -> dict:
    y, sr = librosa.load(path, sr=22050, mono=True)
    dur = len(y) / sr
    # trim=False：不要讓 librosa 砍掉開頭較弱的拍；hop 128 ≈ 5.8ms，拍點時間解析度才夠細
    _, beats = librosa.beat.beat_track(y=y, sr=sr, start_bpm=bpm, units="time", hop_length=HOP, trim=False)
    if len(beats) < BEATS_PER_SHOT:
        raise ValueError("偵測到的拍點太少")
    ibi = float(np.median(np.diff(beats)))
    # 重拍：前 4 拍相位中，拍後 80ms 的 RMS 能量平均最大者（重拍通常有大鼓＋和弦換）
    # ponytail: 啟發式，遇到弱起拍或重拍不特別大聲的曲子可能錯一拍（切點仍在拍上）；要更準再換 downbeat 模型
    win = int(0.08 * sr)
    energy = np.array(
        [np.sqrt(np.mean(y[int(t * sr) : int(t * sr) + win] ** 2)) if t * sr < len(y) else 0 for t in beats]
    )
    k = int(np.argmax([energy[i::4].mean() for i in range(4)]))

    def beat_at(i):  # 偵測不到的尾端拍點用平均拍距外推
        return float(beats[i]) if i < len(beats) else float(beats[-1]) + (i - len(beats) + 1) * ibi

    cuts = [beat_at(k + BEATS_PER_SHOT * i) for i in range(n_shots + 1)]
    if cuts[-1] > dur:
        raise ValueError(f"音樂太短：需要 {cuts[-1]:.2f}s，只有 {dur:.2f}s")
    # ACE-Step 實測會在要求長度前 3–7 秒就演完、後面接靜音，所以檔案夠長不代表有音樂：
    # 以 0.1 秒為單位找最後一段 > -45 dBFS 的位置，必須演奏到最後一個切點
    hop = sr // 10
    loud = [
        i
        for i in range(0, len(y) - hop, hop)
        if 20 * np.log10(np.sqrt(np.mean(y[i : i + hop] ** 2)) + 1e-9) > SILENCE_DB
    ]
    active_end = (loud[-1] + hop) / sr if loud else 0.0
    if active_end < cuts[-1]:
        raise ValueError(f"音樂在 {active_end:.1f}s 就結束了，最後一個切點在 {cuts[-1]:.1f}s")
    return {
        "bpm_measured": round(60 / ibi, 2),
        "t0": cuts[0],
        "cuts": cuts,
        "duration": dur,
        "active_end": round(active_end, 2),
        "n_beats": len(beats),
    }


def frame_bounds(cuts: list[float], fps: int) -> list[int]:
    """切點換成輸出幀數（累積四捨五入，每個切點誤差 ≤ 0.5 幀）。"""
    return [round((c - cuts[0]) * fps) for c in cuts]
