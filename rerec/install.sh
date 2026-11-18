#!/usr/bin/env bash
set -euo pipefail

install_rerec() {
  case $(uname -s) in
    Darwin|Linux) ;;
    *) echo 'rerec 支持 macOS 和 Linux。' >&2; return 1 ;;
  esac
  if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo '请先安装 Python 3.10 或更高版本。' >&2
    return 1
  fi
  local bin="$HOME/.local/bin"
  mkdir -p "$bin"
  tmp=$(mktemp "$bin/.rerec.XXXXXXXX")
  trap 'rm -f "$tmp"' EXIT
  curl -fsSL https://raw.githubusercontent.com/gsh20040816/reclaude-share-billing/main/rerec/rerec -o "$tmp"
  chmod 755 "$tmp"
  "$tmp" --help >/dev/null
  mv -f "$tmp" "$bin/rerec"
  echo "已安装 $bin/rerec"
  case ":${PATH}:" in
    *":$bin:"*) ;;
    *) echo '请在 shell 配置里加上 export PATH="$HOME/.local/bin:$PATH"' ;;
  esac
  if ! command -v reclaude >/dev/null; then
    echo '启动前请先安装官方 reclaude 客户端。'
  fi
}

install_rerec
