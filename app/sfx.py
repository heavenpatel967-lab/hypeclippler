"""Sound effect library.

Put your own sounds (mp3/wav/ogg/m4a) in sfx/<kind>/ folders:
  boom, airhorn, laugh, wow, fail, bruh, suspense, whoosh
The app picks a random file from the folder that fits the moment. Folders that
only have the built-in sound use that one; add your own files to replace it.
Built-in sounds are generated with ffmpeg on first start so the app works offline.
"""
import logging
from pathlib import Path

from . import tools
from .config import ROOT

log = logging.getLogger("sfx")
SFX_DIR = ROOT / "sfx"
KINDS = ["boom", "airhorn", "laugh", "wow", "fail", "bruh", "suspense", "whoosh"]
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}

# ffmpeg lavfi recipes for simple built-in sounds (no downloads needed)
BUILTIN = {
    "boom": ("aevalsrc='0.95*sin(2*PI*55*t*(1-0.35*t))*exp(-3.2*t)+0.4*sin(2*PI*110*t)*exp(-6*t)':d=1.4:s=44100",
             "lowpass=f=500,volume=2.2,alimiter=limit=0.95"),
    "airhorn": ("aevalsrc='0.22*(sgn(sin(2*PI*466*t))+sgn(sin(2*PI*587*t))+0.6*sgn(sin(2*PI*349*t)))"
                "*lt(mod(t,0.5),0.42)':d=1.5:s=44100",
                "highpass=f=300,lowpass=f=3500,afade=t=out:st=1.25:d=0.25"),
    "wow": ("aevalsrc='0.55*sin(2*PI*1568*t)*exp(-3.5*t)+0.3*sin(2*PI*2349*t)*exp(-4.5*t)':d=1.6:s=44100", "anull"),
    "fail": ("aevalsrc='0.35*sin(2*PI*t*if(lt(t,0.45),466,if(lt(t,0.9),440,if(lt(t,1.35),415,"
             "392+5*sin(2*PI*6*t)))))':d=2.4:s=44100",
             "lowpass=f=1800,afade=t=out:st=1.9:d=0.5,aecho=0.6:0.4:40:0.3"),
    "suspense": ("aevalsrc='0.4*sin(2*PI*(150*t+110*t*t))*min(t/1.6,1)*(0.7+0.3*sin(2*PI*9*t))':d=1.8:s=44100",
                 "afade=t=out:st=1.65:d=0.15"),
    "whoosh": ("anoisesrc=d=0.65:c=pink:a=0.8:r=44100",
               "bandpass=f=1400:width_type=h:w=1800,afade=t=in:d=0.3,afade=t=out:st=0.32:d=0.33,volume=1.5"),
}


def ensure_library():
    for kind in KINDS:
        d = SFX_DIR / kind
        d.mkdir(parents=True, exist_ok=True)
        if kind in BUILTIN and not (d / "_builtin.wav").exists():
            src, af = BUILTIN[kind]
            try:
                tools.run([tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", src,
                           "-af", af, "-ac", "2", str(d / "_builtin.wav")], timeout=60)
            except Exception as e:  # noqa: BLE001
                log.warning("could not create built-in %s sound: %s", kind, e)
    readme = SFX_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(__doc__.strip() + "\n\nFree sounds: https://pixabay.com/sound-effects/ "
                          "(check each sound's license before posting).\n", encoding="utf-8")


def available() -> dict:
    """Your own files win; the built-in sound is only used when a folder has nothing else."""
    out = {}
    for kind in KINDS:
        d = SFX_DIR / kind
        if not d.is_dir():
            continue
        files = [p for p in d.iterdir() if p.suffix.lower() in AUDIO_EXT]
        own = [p for p in files if not p.name.startswith("_builtin")]
        chosen = own or files
        if chosen:
            out[kind] = [str(p.resolve()) for p in chosen]
    return out


def mix_args(picks, volume: float):
    """Extra ffmpeg inputs and an audio filter that mixes sounds over input 0's audio.
    Returns (input_args, filter_string, output_label)."""
    inputs, chains, labels = [], [], ["[s0]"]
    chains.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo[s0]")
    for i, p in enumerate(picks, start=1):
        inputs += ["-i", p["file"]]
        ms = int(p["time"] * 1000)
        chains.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                      f"volume={volume:.2f},adelay={ms}|{ms}[s{i}]")
        labels.append(f"[s{i}]")
    chains.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
                  "alimiter=limit=0.95[aout]")
    return inputs, ";".join(chains), "[aout]"
