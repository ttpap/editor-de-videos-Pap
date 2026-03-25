#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "🎬 Editor de Vídeos"
echo "━━━━━━━━━━━━━━━━━━━━━━━━"

# Install dependencies if needed
if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "📦 Instalando dependências..."
  pip3 install -r requirements.txt -q
fi

echo "🚀 Iniciando servidor em http://localhost:8765"
echo "   Pressione Ctrl+C para parar."
echo ""

python3 main.py
