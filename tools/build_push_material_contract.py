"""Build the exact material-contract wrapper used by SK Batch Push."""

from __future__ import annotations

import argparse
import runpy
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = REPO_ROOT / "sk_batch" / "sk_batch_gui.pyw"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("spm")
    args = parser.parse_args()

    namespace = runpy.run_path(str(GUI_PATH))
    app_type = namespace["App"]
    contract_path = app_type._push_material_contract(Path(args.spm).resolve())
    print(contract_path)


if __name__ == "__main__":
    main()
