"""Whisper（faster-whisper）で文字起こし。単体実行すると引数の音声を文字起こしして標準出力に出す。
bot.py はこれを別プロセスで呼ぶ（モデル約2GBを bot に常駐させないため）。"""
import sys

MODEL = "large-v3-turbo"


def load_model():
    from faster_whisper import WhisperModel
    return WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=4)


def transcribe_file(model, path):
    segments, info = model.transcribe(
        path, language="ja", vad_filter=True, condition_on_previous_text=False)
    text = "\n".join(seg.text.strip() for seg in segments).strip()
    return text, info.duration


if __name__ == "__main__":
    text, _ = transcribe_file(load_model(), sys.argv[1])
    print(text)
