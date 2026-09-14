# Tune speech recognition to your voice

Off-the-shelf speech recognition does worst on exactly the speech this plugin hears: an
accent it saw little of in training, and developer vocabulary — `pytest`, `git rebase`,
a file path read aloud. This folder records a dataset of your own voice and explains how
to fine-tune the recogniser on it.

| File | What it is |
|---|---|
| `record.py` | reads each sentence to you and records your takes |
| `sentences.txt` | the 70 sentences, one per line |

## What you need

- **macOS.** The recorder captures through AVFoundation; other platforms are not supported yet.
- **ffmpeg** — `brew install ffmpeg`.
- **An NVIDIA GPU, for training only.** Recording needs nothing beyond the Mac.

## 1. Choose the microphone you already use

Record on the microphone you speak into every day. Training on one and speaking into
another costs much of what fine-tuning buys.

If you pinned one for the plugin with `SPEAK_INPUT_DEVICE`, the recorder uses it and you can
skip ahead. Otherwise the plugin picks a microphone itself, and `/talk` names the one it used
on its first line:

```
[speak] capture: MacBook Pro Microphone: done (gate 0.0225, peak 0.0360)
```

List what this Mac has:

```
uv run voice-tuning/record.py --list-devices
```

The recorder selects microphones by **name**. ffmpeg also numbers them, but the numbers
shift whenever a USB device is plugged in or out, and its first entry is often a webcam
rather than the microphone you use. Skip virtual devices such as `ZoomAudioDevice` — they
carry meeting audio, not your voice.

## 2. Record

```
uv run voice-tuning/record.py --device "MacBook Pro Microphone"
```

Each sentence appears in turn. **Enter** starts a take and **Enter** stops it; then
**Enter** keeps it, **r** records it again, and **q** quits. Quitting is safe: run the same
command again and it resumes at the first take you have not recorded.

### Takes, in passes

Every sentence is recorded **three times** by default (`--takes 5` for more), because one
reading of a sentence teaches the model one version of how you say it.

Takes are recorded **in passes**: every sentence once, then every sentence again. Reading a
sentence three times back to back makes you copy your own previous delivery, which adds
takes without adding variety. For the most useful dataset, **do each pass in a different
sitting** — another time of day, another day — so the recordings capture how your voice
actually changes. That is what the extra takes are for.

### What it writes

Everything goes into one folder, `~/speak-voice-dataset/` unless you pass `--out`:

```
speak-voice-dataset/
  audio/001-1.wav        sentence 1, take 1 — 16 kHz, mono, 16-bit
  audio/001-2.wav
  ...
  manifest_train.jsonl   NeMo manifests, rebuilt every time the recorder exits
  manifest_val.jsonl
```

The folder is self-contained: manifest paths are relative to it, so it can be copied to a
training machine as it is. It lives outside this repository on purpose — it is a recording
of your voice.

**Every tenth sentence is held out** for measuring the result, and all of its takes with it.
The split is by sentence rather than by file because a held-out take of a sentence the model
has already trained on would make the score look better than it is.

**Don't edit `sentences.txt` once you have started recording.** A take's transcript is found
by its line number, so inserting a line relabels every take after it.

### While you read

- **Read two throwaway lines first.** A voice that has just woken up is not the voice you use.
- **Keep the same position** — about a hand's width from the microphone, in the same room.
- **Turn off noise suppression** if anything applies it. It strips frequencies the model
  needs.
- **Stumbled? Press r and read the whole sentence again.** A take patched midway no longer
  matches its transcript.
- **Talk the way you talk to Claude**, not the way you would read to an audience.

## The sentences

| Lines | Group | Why |
|---|---|---|
| 1–20 | Harvard sentences | Broad phonetic coverage. Published by the IEEE in 1969 as phonetically balanced test material, and reproduced freely since. |
| 21–30 | Accent-revealing | Sounds that commonly shift between accents: *th* and *d*, *v* and *w*, *r* and *l*, short and long vowels, final consonant clusters. |
| 31–60 | Talking to a coding assistant | Git, test runners, package managers, error messages, spoken file paths — where general-purpose recognisers fail most. |
| 61–70 | Numbers, acronyms, spelling | Version numbers, ports, HTTP status codes, an IP address, spelled-out identifiers. |

Lines 21–70 are written for this project. Reading a fixed script is what makes the dataset
cheap to build: the transcript of every take is already known, so nothing needs correcting.

## How much is enough

There is no settled number.

- **70 sentences × 3 takes is roughly 15–30 minutes** of speech — enough to measure whether
  fine-tuning helps, and a reasonable first dataset.
- NVIDIA's guide for this model family reports that **a few hours** of in-domain audio closes
  most of the gap.
- Fine-tuning Whisper on **one hour** of in-domain audio took word error rate from 13.1% to
  9.6%; **133 hours** reached only 8.9%. Most of the gain comes early.

Train on what you have, measure it, and record more only if the measurement says it helps.

## 3. Fine-tune

The plugin transcribes with `nemotron-3.5-asr-streaming-0.6b`, an NVIDIA NeMo FastConformer
model served by NeMo-Speech.cpp.

**Use NeMo's own trainer.** Unsloth's speech recognition support covers Whisper only, and
this model is not a Hugging Face `transformers` training target. Training Whisper instead
would also mean replacing the local server, since NeMo-Speech.cpp does not run Whisper.

On a machine with an NVIDIA GPU — a single T4, L4 or A10 handles datasets of this size:

1. **Copy the dataset folder across.** Its manifests use relative paths and work in place.
2. **Read** NVIDIA's [How to Fine-Tune Nemotron 3.5 ASR for Your Language, Domain, or
   Accent](https://huggingface.co/blog/nvidia/fine-tuning-nemotron-35-asr).
3. **Train adapters rather than the whole model.** NVIDIA's full fine-tuning
   [notebook](https://github.com/nvidia-riva/tutorials/blob/main/asr-finetune-nemotron-3.5-asr-streaming-prompt.ipynb)
   targets hundreds of hours of audio. With less than a few hours, use NeMo's
   [adapter configuration](https://github.com/NVIDIA-NeMo/NeMo/blob/main/examples/asr/conf/asr_adapters/asr_adaptation.yaml),
   following this [adapter notebook](https://github.com/nvidia-riva/tutorials/blob/main/asr-finetune-conformer-ctc-adapter-nemo.ipynb):
   a small adapter dimension such as 32, a few hundred steps, and weight decay on. Adapters
   leave the base model's weights untouched, which is what stops a small dataset from eroding
   everything else it recognises.
4. **Measure before and after** on `manifest_val.jsonl`. If word error rate does not fall,
   more training will not fix it; different data might.
5. **Convert** the checkpoint to GGUF with NeMo-Speech.cpp's `convert_model.py` — see
   [model conversion](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/main/docs/model-conversion.md).
6. **Load the converted model** into the local speech server.

### Not yet confirmed

Check both with a short throwaway run before spending hours of GPU time.

- **Whether an adapter checkpoint converts.** NeMo-Speech.cpp's conversion docs do not say
  whether a checkpoint that stores adapters separately from the base weights is supported. If
  it is not, merge the adapters into the base weights before converting, or fine-tune with the
  encoder frozen, which produces an ordinary checkpoint.
- **How the server loads a model you trained.** Its documentation covers named downloads;
  check `nemo-speech serve --help` for loading a local file.

## Privacy

The dataset folder is never uploaded by anything in this project. The plugin's separate
`record` setting (see [`TELEMETRY.md`](../TELEMETRY.md)) keeps recordings of what you say
during real sessions, also only on your machine.

## Sources

- [How to Fine-Tune Nemotron 3.5 ASR](https://huggingface.co/blog/nvidia/fine-tuning-nemotron-35-asr) — NVIDIA
- [nemotron-3.5-asr-streaming-0.6b model card](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b)
- [NeMo `get_full_path`](https://github.com/NVIDIA-NeMo/NeMo/blob/main/nemo/collections/common/parts/preprocessing/manifest.py) — resolves relative manifest paths against the manifest's directory
- [Unsloth speech fine-tuning](https://unsloth.ai/docs/basics/text-to-speech-tts-fine-tuning)
- [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) · [model conversion](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/main/docs/model-conversion.md)
- [Harvard sentences](https://en.wikipedia.org/wiki/Harvard_sentences) · [CMU ARCTIC](http://festvox.org/cmu_arctic/cmu_arctic_report.pdf) · [Speech Accent Archive](https://accent.gmu.edu/)
- [Residual adapters for accented speech](https://arxiv.org/pdf/2109.06952) · [Fine-tuning audio models: 1 hour vs 133 hours](https://kili-technology.com/blog/fine-tuning-audio-models-guide)
- [Microphone and channel mismatch in ASR](https://arxiv.org/html/2508.08967)
- [ivrit.ai: training Whisper for Hebrew](https://www.ivrit.ai/en/2025/02/13/training-whisper/) — a Whisper project, useful for its data practice rather than its tools
