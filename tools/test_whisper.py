"""Whisperモデルの速度・精度比較用。使い方: python test_whisper.py <モデル名>（カレントの test.m4a を処理）"""
import time, sys
from faster_whisper import WhisperModel

model_name = sys.argv[1] if len(sys.argv) > 1 else "large-v3-turbo"
audio = "test.m4a"

t0 = time.time()
model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4)
t1 = time.time()

segments, info = model.transcribe(
    audio,
    language="ja",
    vad_filter=True,
    condition_on_previous_text=False,
)
lines = [f"[{s.start:6.1f}s] {s.text}" for s in segments]
t2 = time.time()

print(f"モデル読込: {t1-t0:.0f}秒")
print(f"音声の長さ: {info.duration:.0f}秒")
print(f"書き起こし: {t2-t1:.0f}秒（{(t2-t1)/info.duration:.2f}倍）")

safe_name = model_name.replace('/', '_')
with open(f"out_{safe_name}.txt", "w") as f:
    f.write("\n".join(lines))
print(f"→ out_{safe_name}.txt に保存")
