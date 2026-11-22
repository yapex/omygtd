#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COPY_SKILL=0
[[ "${1:-}" == "--copy" ]] && COPY_SKILL=1

mkdir -p "$HERE/data" "$HERE/out" "$HERE/snapshots"

# 1. CLI 入口
mkdir -p "$HOME/.local/bin"
ln -sfn "$HERE/bin/gtd" "$HOME/.local/bin/gtd"
echo "linked: ~/.local/bin/gtd -> $HERE/bin/gtd"

# 2. skill
SKILL_DIR="$HOME/.hermes/skills/gtd"
if [[ $COPY_SKILL -eq 1 ]]; then
  rm -rf "$SKILL_DIR"
  mkdir -p "$SKILL_DIR"
  cp -R "$HERE/skill/." "$SKILL_DIR/"
  echo "copied skill: $HERE/skill -> $SKILL_DIR （容器 profile 用这个）"
else
  mkdir -p "$(dirname "$SKILL_DIR")"
  ln -sfn "$HERE/skill" "$SKILL_DIR"
  echo "linked skill: $SKILL_DIR -> $HERE/skill （default profile 用这个）"
fi

# 3. 初始化数据库
if command -v gtd >/dev/null 2>&1; then
  gtd init
else
  echo "warn: gtd 未在 PATH 中，跳过 init"
fi

echo "done."
