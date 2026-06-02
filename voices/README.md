# Piper TTS voices

Drop neural **Piper** voice models into this folder to make them selectable
under **SPEECH → PIPER VOICES** in the terminal.

## Installing a voice

Each voice is two files that share the same stem:

```
voices/
├── en_US-amy-medium.onnx        ← the model weights
├── en_US-amy-medium.onnx.json   ← the model config (sample rate, phonemes…)
└── previews/                    ← auto-generated .wav previews (created for you)
```

1. Install the engine: `pip install piper-tts`
2. Download a voice (`.onnx` + `.onnx.json`) from the Piper voice catalogue
   (e.g. <https://huggingface.co/rhasspy/piper-voices>) and place **both**
   files here.
3. Open the terminal, go to the **SPEECH** tab, set the engine to **PIPER**,
   and press **GENERATE PREVIEWS**. A short sample is rendered for every voice
   that does not have one yet and appears next to it — click ▶ to audition.
4. Select a voice and enable **VOICE FEEDBACK**.

The voice id shown in the UI is the filename stem (e.g. `en_US-amy-medium`).
Model weights and generated previews are git-ignored; this folder and its
README are kept so the structure is always present.
