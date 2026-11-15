"""Makes the decision model spending labels use (config.decision_model): Decider-2b quantized to 4 bits.

The published Decider-2b is bf16 only, 3.8 GB, and takes 4.7 GB while running; 4 bits makes it 1.1 GB
and 2.5 GB. Run it with Rapid-MLX's own Python, which has MLX (the app's venv doesn't), from the repo:

    ~/.local/share/uv/tools/rapid-mlx/bin/python src/model/quantize_decider.py ~/.local/share/deckartes/models/decider-2b-4bit

It downloads the original into the Hugging Face cache (delete it afterwards to free 3.8 GB) and writes
a checkpoint `rapid-mlx system-one <folder> --backend decider` serves. The whole model is 4 bits, the
embedding table too: keeping that bf16 cost 0.7 GB and labeled no better. Details in docs/model-server.md.
"""
# pyright: reportMissingImports=false

import json
import shutil
import sys
from pathlib import Path

from huggingface_hub import snapshot_download
from mlx import nn
from mlx_lm.models import qwen3_5
from mlx_lm.utils import load_model, save_model

# The release Rapid-MLX's decider backend pins, so this matches what it was checked against
REPOSITORY = "nativ-community/decider-2b"
REVISION = "acbae4ecce4dbcc0aea8c5a501c9f54008458a70"
BITS = 4
GROUP_SIZE = 64


def main(target: Path) -> None:
    source = Path(snapshot_download(REPOSITORY, revision=REVISION))
    # Loaded the way Rapid-MLX loads it: a Qwen3.5 text model whose config says model_type decider2
    model, _ = load_model(
        source,
        model_config={"model_file": None, "auto_map": None},
        get_model_classes=lambda config: (qwen3_5.Model, qwen3_5.ModelArgs),
    )
    nn.quantize(
        model,
        group_size=GROUP_SIZE,
        bits=BITS,
        class_predicate=lambda _, layer: hasattr(layer, "to_quantized") and layer.weight.shape[-1] % GROUP_SIZE == 0,
    )
    target.mkdir(parents=True, exist_ok=True)
    # The tokenizer, chat template and the release's calibration (decision_config in config.json)
    for file in source.iterdir():
        if file.suffix != ".safetensors" and file.name != "model.safetensors.index.json":
            shutil.copy(file, target / file.name)
    save_model(target, model, donate_model=True)
    config = json.loads((source / "config.json").read_text())
    config["quantization"] = {"group_size": GROUP_SIZE, "bits": BITS}
    (target / "config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False))
    size = sum(file.stat().st_size for file in target.glob("*.safetensors"))
    print(f"Wrote {target}: {size / 1e9:.2f} GB of weights")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <folder to write the model to>")
    main(Path(sys.argv[1]).expanduser())
