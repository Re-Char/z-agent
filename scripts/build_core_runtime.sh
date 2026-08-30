#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "$0")/.." && pwd)"
runtime_output="$repository_root/dist/core-runtime"
temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/zagent-core-runtime.XXXXXX")"
runtime_prefix="$temporary_root/env"
runtime_archive="$temporary_root/core-runtime.tar.gz"

cleanup() {
  rm -rf "$temporary_root"
}
trap cleanup EXIT

conda env create --prefix "$runtime_prefix" --file "$repository_root/environment-runtime.yml"
"$runtime_prefix/bin/python" -m pip install --no-deps --no-cache-dir "$repository_root"

pack_command="${ZAGENT_CONDA_PACK:-$repository_root/.conda/envs/zagent/bin/conda-pack}"
if [[ ! -x "$pack_command" ]]; then
  echo "conda-pack not found: $pack_command" >&2
  exit 2
fi
"$pack_command" --prefix "$runtime_prefix" --output "$runtime_archive"

if [[ "$runtime_output" != "$repository_root/dist/core-runtime" ]]; then
  echo "refusing unexpected runtime output: $runtime_output" >&2
  exit 2
fi
if [[ -e "$runtime_output" ]]; then
  rm -rf "$runtime_output"
fi
mkdir -p "$runtime_output"
tar -xzf "$runtime_archive" -C "$runtime_output"

"$runtime_output/bin/python" -c \
  "import cryptography, fastapi, httpx, jieba, opencc, pydantic, uvicorn, zagent; print(zagent.__version__)"
