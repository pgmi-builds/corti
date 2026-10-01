#!/usr/bin/env bash
# corti — one-command Docker install (pull pre-built image)
# Usage: curl -fsSL https://raw.githubusercontent.com/pgmi-builds/corti/main/install.sh | bash
#   ... | bash -s -- --only-dsh   → install only the DeepSeek Harness plugin
#   ... | bash -s -- --only-hermes / --only-claude → single-integration mode
# (single-integration modes skip Docker/server setup; they expect a Corti
#  server already reachable and only wire the agent side)
set -euo pipefail

IMAGE="m1research/corti"

# Tag selector (first positional argument).
#   curl ... | bash              → latest      (all-in-one, embedded PG)
#   curl ... | bash -s slim      → slim        (external PG required)
#   curl ... | bash -s v0.3.4    → v0.3.4      (pinned version, full)
#   curl ... | bash -s v0.3.4-slim → v0.3.4-slim (pinned version, slim)
TAG="${1:-latest}"
IMAGE_FULL="${IMAGE}:${TAG}"

# ── Integration selector flags ────────────────────────────────────────────
# Default: full install (server + all detected agent integrations).
# --only-dsh / --only-hermes / --only-claude: wire exactly one agent,
# skip Docker pull/run and server-side setup (Corti server must already
# be running somewhere reachable).
ONLY=""
for arg in "$@"; do
    case "$arg" in
        --only-dsh|--only-hermes|--only-claude) ONLY="${arg#--only-}";;
    esac
done
if [ -n "$ONLY" ]; then
    # Arg 1 may have been the flag itself; don't treat it as an image tag.
    case "$TAG" in
        --only-*|"") TAG="latest";;
    esac
    IMAGE_FULL="${IMAGE}:${TAG}"
fi

DSH_HOME_DIR="${DSH_HOME:-$HOME/.dsh}"
DSH_ENV_FILE="$DSH_HOME_DIR/.env"
CORTI_BASE_URL="${CORTI_BASE_URL:-http://127.0.0.1:5473}"

# ── Colors ────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'
    CYAN='\033[0;36m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    BOLD=''; GREEN=''; YELLOW=''; RED=''; CYAN=''; BLUE=''; NC=''
fi

print_banner() {
    cat <<'BANNER'

   ██████╗ ██████╗ ██████╗ ████████╗██╗
  ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██║
  ██║     ██║   ██║██████╔╝   ██║   ██║
  ██║     ██║   ██║██╔══██╗   ██║   ██║
  ╚██████╗╚██████╔╝██║  ██║   ██║   ██║
   ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝

BANNER
    printf "  ${BOLD}Corti${NC} — ${GREEN}Multi-agent memory for AI swarms${NC}\n"
    echo ""
}

# ── Helpers ────────────────────────────────────────────────────────────────
info()    { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
error()   { echo -e "${RED}✗${NC} $*"; }
section() { echo ""; echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${YELLOW}  $*${NC}"; echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── Main ──────────────────────────────────────────────────────────────────
print_banner

# ── Step 0: Detect platform ───────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux*)  PLATFORM="linux";;
    Darwin*) PLATFORM="macos";;
    *)       error "Unsupported OS: $OS"; exit 1;;
esac
info "Platform: $PLATFORM ($ARCH)"

# ── Step 1: Check Docker ──────────────────────────────────────────────────
section "Step 1: Check Docker"

if [ -n "$ONLY" ]; then
    info "Single-integration mode ($ONLY) — skipping Docker/server steps"
    SKIP_SERVER=1
elif command -v docker &>/dev/null; then
    info "Docker $(docker --version | cut -d' ' -f3 | cut -d',' -f1)"
else
    error "Docker is required but not found."
    echo ""
    if [ "$PLATFORM" = "macos" ]; then
        echo "  brew install --cask docker"
    else
        echo "  curl -fsSL https://get.docker.com | bash"
    fi
    exit 1
fi

# ── Step 2: Pull image ────────────────────────────────────────────────────
section "Step 2: Pull Image"

if [ -n "${SKIP_SERVER:-}" ]; then
    info "Skipping image pull (single-integration mode)"
else
    info "Pulling $IMAGE_FULL..."
    docker pull "$IMAGE_FULL"
fi

# ── Step 3: Seed data directory ───────────────────────────────────────────
section "Step 3: Data Directory"

DATA_DIR="${CORTI_ROOT:-$HOME/.corti}"

if [ -n "${SKIP_SERVER:-}" ]; then
    info "Skipping data directory seeding (single-integration mode)"
elif [ -d "$DATA_DIR" ] && [ -f "$DATA_DIR/corti.toml" ]; then
    info "Config already exists: $DATA_DIR/corti.toml"
else
    info "Seeding data directory at $DATA_DIR"
    mkdir -p "$DATA_DIR/.index/sqlite" "$DATA_DIR/.index/pg" "$DATA_DIR/.tmp"

    # Extract default configs from the Docker image.
    CID=$(docker create "$IMAGE_FULL")
    docker cp "$CID:/opt/corti/config/default.toml" "$DATA_DIR/corti.toml"
    docker cp "$CID:/opt/corti/config/default_ome.toml" "$DATA_DIR/ome.toml"
    docker rm "$CID" >/dev/null
fi

# ── Step 4: Agent integrations ────────────────────────────────────────────
section "Step 4: Agent Integrations"

CID=""
if [ -z "${SKIP_SERVER:-}" ]; then
    CID=$(docker create "$IMAGE_FULL")
fi

# ── 4d: DeepSeek Harness (dsh) ──────────────────────────────────────────────
# Unlike Hermes/Claude (docker cp of the plugin dir), dsh installs plugins
# through its own pnpm-backed profile manager from a distributable source.
# The canonical source is the corti GitHub repo (the dsh-plugin-topic listed
# integration), so every machine gets the same committed, prebuilt dist/.
install_dsh() {
    if ! command -v dsh &>/dev/null; then
        info "DeepSeek Harness (dsh) not detected — skipping plugin install"
        echo "  Install: npm install -g @deepseek-ai/dsh   (https://github.com/deepseek-ai/deepseek-harness)"
        return 0
    fi
    info "DeepSeek Harness (dsh) detected"

    # a) env wiring: ~/.dsh/.env (layered launch env, survives upgrades)
    mkdir -p "$DSH_HOME_DIR"
    if [ -f "$DSH_ENV_FILE" ] && grep -q '^CORTI_BASE_URL=' "$DSH_ENV_FILE"; then
        info "Corti env already present in $DSH_ENV_FILE"
    else
        {
            echo ""
            echo "# ── Corti memory plugin (added by corti install.sh) ──"
            echo "CORTI_BASE_URL=$CORTI_BASE_URL"
            echo "CORTI_AGENT_ID=pc-deepseek-default"
        } >> "$DSH_ENV_FILE"
        info "Corti env appended to $DSH_ENV_FILE (CORTI_BASE_URL=$CORTI_BASE_URL)"
    fi

    # b) plugin install into every existing dsh profile that bundles dsh-base
    SRC="${CORTI_DSH_SOURCE:-https://github.com/pgmi-builds/corti#src/integrations/deepseek-harness}"
    local installed=0
    for profile_dir in "$DSH_HOME_DIR"/profiles/*/; do
        [ -f "$profile_dir/package.json" ] || continue
        profile=$(basename "$profile_dir")
        if dsh plugin --profile "$profile" add "$SRC" >/dev/null 2>&1; then
            info "corti-memory installed into dsh profile: $profile"
            installed=$((installed+1))
        else
            warn "dsh profile '$profile': plugin add failed — run manually:"
            echo "    dsh plugin --profile $profile add $SRC"
        fi
    done
    if [ "$installed" -eq 0 ] && ! ls "$DSH_HOME_DIR"/profiles/*/ >/dev/null 2>&1; then
        info "No dsh profiles yet — the plugin activates on first 'dsh web' init"
        echo "  After first run: dsh plugin --profile web add $SRC"
    fi
}

install_hermes() {
# ── 4a: Hermes ──────────────────────────────────────────────────────────────
# (docker-cp based: requires the pulled image, i.e. full-install mode)
HERMES_PLUGIN_DIR="$HOME/.hermes/plugins"
HERMES_TARGET="$HERMES_PLUGIN_DIR/corti"

if [ -n "${SKIP_SERVER:-}" ]; then
    info "Skipping Hermes integration (single-integration mode: $ONLY)"
    return 0
fi
if command -v hermes &>/dev/null; then
    info "Hermes detected"
    if [ -L "$HERMES_TARGET" ]; then
        warn "Old symlink found at $HERMES_TARGET — removing"
        rm "$HERMES_TARGET"
    fi
    if [ -d "$HERMES_TARGET" ]; then
        info "Hermes plugin already installed at $HERMES_TARGET"
        echo "  Remove it manually if you want a fresh install: rm -rf $HERMES_TARGET"
    else
        mkdir -p "$HERMES_PLUGIN_DIR"
        docker cp "$CID:/opt/corti/integrations/hermes" "$HERMES_PLUGIN_DIR/"
        mv "$HERMES_PLUGIN_DIR/hermes" "$HERMES_TARGET"
        info "Hermes plugin installed: $HERMES_TARGET"
    fi
    # Auto-enable the plugin + set memory provider so it's active immediately.
    # NOTE: `grep -c ... >/dev/null` instead of `grep -q`: under `set -o
    # pipefail` (top of script) grep -q exits as soon as it matches, closing
    # the pipe and SIGPIPE-ing `hermes plugins list` — the whole condition
    # would silently evaluate false and the enable would never run.
    if hermes plugins list 2>/dev/null | grep -c corti >/dev/null; then
        # --no-allow-tool-override: skips the interactive "allow tool
        # override?" prompt. That prompt reads from stdin, which under
        # `curl ... | bash` IS the script pipe — the prompt would swallow
        # the remaining script bytes and the install would silently
        # truncate (exit 0) right here. Corti's plugin doesn't need
        # built-in tool overrides, so never grant them and never prompt.
        if hermes plugins enable --no-allow-tool-override corti 2>/dev/null; then
            info "Hermes plugin enabled"
        else
            warn "Hermes plugin found but enable failed — run: hermes plugins enable corti"
        fi
    fi
    # Also set the memory provider (belt-and-suspenders — some Hermes versions
    # require this even after plugin enable). No `config get` pre-check: the
    # CLI has no `get` subcommand (show/edit/set only), and `config set` is
    # idempotent — set unconditionally, warn only on failure.
    if hermes config set memory.provider corti 2>/dev/null; then
        info "Hermes memory.provider set to corti"
    else
        warn "Could not set memory.provider. Run: hermes config set memory.provider corti"
    fi
else
    info "Hermes not detected — skipping plugin install"
    echo "  Install Hermes: https://github.com/NousResearch/hermes-agent"
fi
}

install_claude() {
# ── 4b: Claude Code ─────────────────────────────────────────────────────────
# ~/.claude/skills/ is auto-discovered by Claude Code — any folder containing
# .claude-plugin/plugin.json is loaded as a plugin on the next session with
# zero CLI commands.  hooks/hooks.json and MCP are auto-discovered.
CLAUDE_SKILLS_DIR="$HOME/.claude/skills"
CLAUDE_TARGET="$CLAUDE_SKILLS_DIR/corti"

if [ -n "${SKIP_SERVER:-}" ]; then
    info "Skipping Claude Code integration (single-integration mode: $ONLY)"
    return 0
fi
if command -v claude &>/dev/null; then
    info "Claude Code detected"
    if [ -L "$CLAUDE_TARGET" ]; then
        warn "Old symlink found at $CLAUDE_TARGET — removing"
        rm "$CLAUDE_TARGET"
    fi
    if [ -d "$CLAUDE_TARGET" ]; then
        info "Claude Code plugin already installed at $CLAUDE_TARGET"
        echo "  Remove it manually if you want a fresh install: rm -rf $CLAUDE_TARGET"
    else
        mkdir -p "$CLAUDE_SKILLS_DIR"
        docker cp "$CID:/opt/corti/integrations/claude-code" "$CLAUDE_SKILLS_DIR/"
        mv "$CLAUDE_SKILLS_DIR/claude-code" "$CLAUDE_TARGET"
        info "Claude Code plugin installed: $CLAUDE_TARGET"
        info "Hooks + MCP auto-discovered on next session — no manual steps needed"
    fi
else
    info "Claude Code not detected — skipping plugin install"
    echo "  Install Claude Code: https://claude.ai/code"
fi
}

# dispatcher: run selected integrations
if [ "$ONLY" = "dsh" ]; then
    install_dsh
elif [ "$ONLY" = "hermes" ]; then
    install_hermes
elif [ "$ONLY" = "claude" ]; then
    install_claude
else
    install_hermes
    install_claude
    install_dsh
fi

if [ -n "$CID" ]; then
    docker rm "$CID" >/dev/null
fi

# ── Step 5: API keys ─────────────────────────────────────────────────────
if [ -n "${SKIP_SERVER:-}" ]; then
    section "Done (single-integration mode: $ONLY)"
    echo ""
    echo -e "  ${BOLD}Verify:${NC} ask the agent to list its memory_* tools."
    echo ""
    exit 0
fi
section "Step 5: Configure API Keys"

echo ""
echo -e "  ${BOLD}${RED}⚠  API keys are required.${NC} Corti ships with ${BOLD}empty keys${NC}"
echo -e "  pointing at ${BOLD}OpenAI${NC} (https://api.openai.com/v1). Nothing works"
echo -e "  until you add your own key:"
echo ""
echo -e "  ${BOLD}${RED}⚠  The server will NOT start without a valid key.${NC} Corti"
echo -e "  fails fast at boot when [llm] / [embedding] keys are missing — this is"
echo -e "  intentional: configure your keys BEFORE the first docker run."
echo ""
echo -e "  ${BOLD}1. Edit the seeded config:${NC}"
echo -e "     ${CYAN}\$EDITOR $DATA_DIR/corti.toml${NC}"
echo -e "        [llm]       api_key = \"sk-...\"   # model: gpt-4.1-mini"
echo -e "        [embedding] api_key = \"sk-...\"   # model: text-embedding-3-small"
echo -e "        [rerank]    api_key = \"sk-...\"   # optional"
echo ""
echo -e "  ${BOLD}2. Or pass env vars to docker run (Step 6):${NC}"
echo -e "     ${CYAN}CORTI_LLM__API_KEY=sk-... CORTI_EMBEDDING__API_KEY=sk-...${NC}"
echo ""

# ── Step 6: Start the server ────────────────────────────────────────────
section "Step 6: Start Server"

CONTAINER_NAME="corti"

# Slim mode: external PostgreSQL required.
if [ "$TAG" = "slim" ]; then
    info "Slim mode: external PostgreSQL required"
    missing=""
    for var in DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD; do
        if [ -z "${!var:-}" ]; then
            missing="$missing  $var\n"
        fi
    done
    if [ -n "$missing" ]; then
        warn "Slim mode needs these env vars set before docker run:"
        printf "$missing"
        echo "  export DB_HOST=192.168.1.100 DB_PORT=5432 DB_NAME=corti DB_USER=corti DB_PASSWORD=xxx"
    else
        info "External PostgreSQL at ${DB_HOST}:${DB_PORT}/${DB_NAME}"
    fi
fi

echo ""
echo -e "  ${BOLD}Install complete. Start Corti with:${NC}"
echo ""
if [ "$TAG" = "slim" ]; then
    echo -e "  ${CYAN}docker run -d --name $CONTAINER_NAME -p 5473:5473 \\"
    echo -e "    -v $DATA_DIR:/home/app/.corti \\"
    echo -e "    -e DB_HOST=$DB_HOST -e DB_PORT=$DB_PORT -e DB_NAME=$DB_NAME \\"
    echo -e "    -e DB_USER=$DB_USER -e DB_PASSWORD=$DB_PASSWORD $IMAGE_FULL${NC}"
else
    echo -e "  ${CYAN}docker run -d --name $CONTAINER_NAME -p 5473:5473 \\"
    echo -e "    -v $DATA_DIR:/home/app/.corti $IMAGE_FULL${NC}"
fi
echo ""
echo -e "  ${YELLOW}⚠ Configured your API keys first? If not, see Step 5 above.${NC}"
echo ""
echo -e "  ${BOLD}Check:${NC}     curl http://localhost:5473/health"
echo -e "  ${BOLD}Logs:${NC}      docker logs -f $CONTAINER_NAME"
echo -e "  ${BOLD}Restart:${NC}   docker restart $CONTAINER_NAME   ${CYAN}# after editing corti.toml${NC}"
echo -e "  ${BOLD}Docs:${NC}     https://cortistrate.dev/docs"
echo ""
