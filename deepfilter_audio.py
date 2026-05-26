import asyncio
import numpy as np
import torch
from math import gcd
from scipy.signal import resample_poly
from df.enhance import enhance, init_df
from livekit import rtc

DF_SAMPLE_RATE = 48000  # DeepFilterNet native rate
CHUNK_MS = 20           # process in 20ms chunks — balances latency vs quality


class DeepFilterProcessor:
    """
    Wraps DeepFilterNet for real-time per-chunk audio enhancement.
    Maintains df_state between chunks so the model has temporal context.
    """

    def __init__(self):
        print("[DEEPFILTER] Loading model — this takes ~5s on first run...")
        self._model, self._df_state, _ = init_df()
        print("[DEEPFILTER] Model ready.")

    def process_frame(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        src_rate = frame.sample_rate
        pcm = np.frombuffer(bytes(frame.data), dtype=np.int16).astype(np.float32) / 32768.0

        # Upsample to DeepFilterNet's native 48kHz if needed
        if src_rate != DF_SAMPLE_RATE:
            g = gcd(DF_SAMPLE_RATE, src_rate)
            pcm = resample_poly(pcm, DF_SAMPLE_RATE // g, src_rate // g)

        tensor = torch.from_numpy(pcm).unsqueeze(0)  # [1, T]
        enhanced = enhance(self._model, self._df_state, tensor)
        enhanced_np = enhanced.squeeze(0).detach().numpy()

        # Downsample back to original rate
        if src_rate != DF_SAMPLE_RATE:
            g = gcd(src_rate, DF_SAMPLE_RATE)
            enhanced_np = resample_poly(enhanced_np, src_rate // g, DF_SAMPLE_RATE // g)

        out = (enhanced_np * 32768.0).clip(-32768, 32767).astype(np.int16)

        return rtc.AudioFrame(
            data=out.tobytes(),
            sample_rate=src_rate,
            num_channels=frame.num_channels,
            samples_per_channel=len(out),
        )


async def run_filter_pipeline(
    track: rtc.RemoteAudioTrack,
    source: rtc.AudioSource,
    processor: DeepFilterProcessor,
):
    """
    Reads frames from the caller's track, filters each one through DeepFilterNet,
    and pushes the result into the filtered AudioSource.
    """
    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
    async for event in stream:
        try:
            filtered = processor.process_frame(event.frame)
            await source.capture_frame(filtered)
        except Exception as e:
            # On error pass the raw frame so the call doesn't drop
            print(f"[DEEPFILTER] Frame error — passing raw audio: {e}")
            await source.capture_frame(event.frame)
