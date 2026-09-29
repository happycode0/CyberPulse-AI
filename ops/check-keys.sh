#!/usr/bin/env bash
# Verify every credential in .env works. Prints status and non-sensitive
# metadata only — no key, token or password is ever echoed.
#
#   ./ops/check-keys.sh
#
# Exit code 0 if everything required for Stage 1 passes.

set -uo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "No .env found. Run:  cp .env.example .env && chmod 600 .env"
  exit 1
fi

# Load .env without echoing it.
set -a; . ./.env; set +a

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[90m–\033[0m %s\n' "$1"; SKIP=$((SKIP+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Warn if .env is readable by others. On a Windows-mounted filesystem (WSL
# /mnt/c) chmod is a no-op, so report that accurately instead of false-alarming.
fstype=$(stat -f -c '%T' . 2>/dev/null || echo unknown)
perms=$(stat -c '%a' .env 2>/dev/null || echo "?")
if [[ "$fstype" == "v9fs" || "$fstype" == "9p" || "$fstype" == "drvfs" || "$perms" == "777" && -e /proc/version && $(grep -ci microsoft /proc/version) -gt 0 ]]; then
  printf '\033[33m!\033[0m Repo is on a Windows filesystem, so Unix permissions on .env are not enforced\n'
  printf '  (reported %s). The file is protected by Windows ACLs instead. For stronger\n' "$perms"
  printf '  isolation, keep the repo inside the WSL filesystem (e.g. ~/CyberPulse-AI).\n'
elif [[ "$perms" != "600" ]]; then
  printf '\033[33m!\033[0m .env permissions are %s, expected 600. Run: chmod 600 .env\n' "$perms"
fi

jqr() { jq -r "$1" 2>/dev/null; }

# ─── OpenRouter ──────────────────────────────────────────────────────────────
head_ "OpenRouter"
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  bad "OPENROUTER_API_KEY not set"
else
  r=$(curl -sS -m 20 https://openrouter.ai/api/v1/key \
        -H "Authorization: Bearer ${OPENROUTER_API_KEY}")
  if [[ "$(echo "$r" | jqr '.data.label // empty')" != "" ]]; then
    lim=$(echo "$r" | jqr '.data.limit // "unlimited"')
    rem=$(echo "$r" | jqr '.data.limit_remaining // "n/a"')
    rst=$(echo "$r" | jqr '.data.limit_reset // "never"')
    ud=$(echo  "$r" | jqr '.data.usage_daily // 0')
    fu=$(echo  "$r" | jqr '.data.free_model_daily_requests.used // "?"')
    fl=$(echo  "$r" | jqr '.data.free_model_daily_requests.limit // "?"')
    ok "key valid — limit \$${lim} (reset ${rst}), remaining \$${rem}, today \$${ud}"
    ok "free-model quota today: ${fu}/${fl} requests used"
    [[ "$lim" == "unlimited" ]] && printf '\033[33m!\033[0m no credit limit on this key. Set one to 20 (monthly) so spend is capped at the provider.\n'
    [[ "$fl" == "50" ]] && printf '\033[33m!\033[0m free tier capped at 50/day. Purchasing 10 credits raises it to 1000/day, which tier 0 depends on.\n'
  else
    bad "key rejected: $(echo "$r" | jqr '.error.message // "unknown error"')"
  fi

  n=$(curl -sS -m 20 https://openrouter.ai/api/v1/models | jqr '.data | length')
  [[ "${n:-0}" -gt 0 ]] && ok "/models reachable — ${n} models listed" || bad "/models unreachable"

  b=$(curl -sS -m 30 "https://openrouter.ai/api/v1/benchmarks?source=artificial-analysis&max_results=1" \
        -H "Authorization: Bearer ${OPENROUTER_API_KEY}" | jqr '.meta.model_count // empty')
  [[ -n "$b" ]] && ok "/benchmarks reachable (RIPPERDOC quality signal)" || bad "/benchmarks failed"

  d=$(curl -sS -m 30 "https://openrouter.ai/api/v1/datasets/rankings-daily?period=week&modality=tool_calling" \
        -H "Authorization: Bearer ${OPENROUTER_API_KEY}" | jqr '.data | length')
  [[ "${d:-0}" -gt 0 ]] && ok "/datasets/rankings-daily reachable — ${d} rows (adoption signal)" || bad "/datasets/rankings-daily failed"

  s=$(curl -sS -m 30 "https://openrouter.ai/api/v1/datasets/session-cost?turn_range=1-turn&limit=5" \
        -H "Authorization: Bearer ${OPENROUTER_API_KEY}" | jqr '.data | length')
  [[ "${s:-0}" -gt 0 ]] && ok "/datasets/session-cost reachable — ${s} cells (real cost-per-task signal)" || bad "/datasets/session-cost failed"
fi

# ─── Model ladder: verify every configured model against the price ceiling ────
head_ "Model ladder vs \$${MAX_OUTPUT_PRICE_PER_MTOK:-1.00}/M output ceiling"
if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
  all=$(curl -sS -m 20 https://openrouter.ai/api/v1/models)
  ceil=${MAX_OUTPUT_PRICE_PER_MTOK:-1.00}
  for var in MODEL_TIER0_FREE MODEL_TIER1_CHEAP MODEL_TIER2_STRONG MODEL_CODE MODEL_AUDIT; do
    chain="${!var:-}"
    [[ -z "$chain" ]] && { skip "$var not set"; continue; }
    IFS=',' read -ra models <<< "$chain"
    for m in "${models[@]}"; do
      m=$(echo "$m" | xargs)
      row=$(echo "$all" | jq -r --arg s "$m" '.data[]|select(.id==$s)|
        "\((.pricing.completion|tonumber)*1000000)|\((.pricing.prompt|tonumber)*1000000)|\(
          if (((.supported_parameters//[])|index("structured_outputs")) and ((.supported_parameters//[])|index("tools"))) then "cap" else "NOCAP" end)"' 2>/dev/null)
      if [[ -z "$row" ]]; then
        bad "$var: $m — NOT FOUND on OpenRouter (withdrawn?)"
        continue
      fi
      out=${row%%|*}; rest=${row#*|}; in=${rest%%|*}; cap=${rest##*|}
      over=$(awk -v a="$out" -v b="$ceil" 'BEGIN{print (a>b)?1:0}')
      if [[ "$over" == "1" ]]; then
        bad "$var: $m — output \$${out}/M EXCEEDS ceiling \$${ceil}"
      elif [[ "$cap" == "NOCAP" ]]; then
        bad "$var: $m — lacks tools+structured_outputs (unusable)"
      else
        ok "$var: $m — in \$${in} / out \$${out} per M"
      fi
    done
  done
else
  skip "needs OPENROUTER_API_KEY"
fi

# ─── Tavily ──────────────────────────────────────────────────────────────────
head_ "Tavily"
if [[ -z "${TAVILY_API_KEY:-}" ]]; then
  bad "TAVILY_API_KEY not set"
else
  r=$(curl -sS -m 20 https://api.tavily.com/usage -H "Authorization: Bearer ${TAVILY_API_KEY}")
  u=$(echo "$r" | jqr '.key.usage // empty')
  if [[ -n "$u" ]]; then
    ok "key valid — key usage ${u}/$(echo "$r" | jqr '.key.limit // "?"'), plan $(echo "$r" | jqr '.account.current_plan // "?"') $(echo "$r" | jqr '.account.plan_usage // "?"')/$(echo "$r" | jqr '.account.plan_limit // "?"') credits"
  else
    bad "rejected: $(echo "$r" | jqr '.detail.error // .detail // "unknown"')"
  fi
fi

# ─── GitHub ──────────────────────────────────────────────────────────────────
head_ "GitHub"
if [[ -z "${CYBERPULSE_PUBLISH_TOKEN:-}" ]]; then
  bad "CYBERPULSE_PUBLISH_TOKEN not set"
else
  GH=(-H "Authorization: Bearer ${CYBERPULSE_PUBLISH_TOKEN}" -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28")
  rl=$(curl -sS -m 20 "${GH[@]}" https://api.github.com/rate_limit | jqr '.resources.core.limit // empty')
  if [[ "${rl:-0}" -gt 1000 ]]; then
    ok "token valid — core rate limit ${rl}/hour (authenticated)"
  elif [[ -n "$rl" ]]; then
    bad "token appears unauthenticated — rate limit only ${rl}/hour"
  else
    bad "token rejected"
  fi
  repo=$(curl -sS -m 20 "${GH[@]}" "https://api.github.com/repos/${GITHUB_REPOSITORY:-happycode0/CyberPulse-AI}")
  vis=$(echo "$repo" | jqr '.visibility // empty')
  if [[ -n "$vis" ]]; then
    ok "repo reachable — visibility: ${vis}, default branch: $(echo "$repo" | jqr '.default_branch')"
    [[ "$vis" == "private" ]] && printf '\033[33m!\033[0m repo is private. GitHub Pages needs a public repo on the Free plan.\n'
  else
    bad "cannot read ${GITHUB_REPOSITORY:-repo} — check the token scope"
  fi
  adv=$(curl -sS -m 20 "${GH[@]}" "https://api.github.com/advisories?per_page=1" | jqr 'length')
  [[ "${adv:-0}" -ge 1 ]] && ok "Advisory Database reachable (LIBRARIAN ground truth)" || bad "/advisories failed"
fi

# ─── NVD (optional) ──────────────────────────────────────────────────────────
head_ "NVD (optional)"
if [[ -z "${NVD_API_KEY:-}" ]]; then
  skip "NVD_API_KEY not set — fine; severity comes from CNA + CISA Vulnrichment"
else
  t=$(curl -sS -m 30 -H "apiKey: ${NVD_API_KEY}" \
      "https://services.nvd.nist.gov/rest/json/cves/2.0?resultsPerPage=1" | jqr '.totalResults // empty')
  [[ -n "$t" ]] && ok "key valid — ${t} CVEs indexed" || bad "key rejected or rate limited"
fi

# ─── Telegram ────────────────────────────────────────────────────────────────
head_ "Telegram"
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  skip "TELEGRAM_BOT_TOKEN not set"
else
  me=$(curl -sS -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jqr '.result.username // empty')
  if [[ -n "$me" ]]; then
    ok "bot valid — @${me}"
    if [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
      s=$(curl -sS -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=CyberPulse-AI: credential check OK. LINK is wired up." | jqr '.ok // empty')
      [[ "$s" == "true" ]] && ok "test message delivered to chat ${TELEGRAM_CHAT_ID}" || bad "send failed — check TELEGRAM_CHAT_ID"
    else
      skip "TELEGRAM_CHAT_ID not set"
    fi
  else
    bad "bot token rejected"
  fi
fi

# ─── Ground-truth sources needing no credentials ─────────────────────────────
head_ "Open sources (no credentials needed)"
UA="CyberPulse-AI/1.0 (+https://github.com/happycode0/CyberPulse-AI)"
# --http1.1: cyber.gov.au intermittently aborts HTTP/2 streams with
# INTERNAL_ERROR. httpx defaults to HTTP/1.1 so the collector is unaffected,
# but curl negotiates h2 and hits it.
probe() {
  c=$(curl -sS -m 25 --http1.1 -o /dev/null -w '%{http_code}' -A "$UA" "$2")
  [[ "$c" == "200" ]] && ok "$1" || bad "$1 (HTTP $c)"
}
probe "CISA KEV"         "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
probe "cvelistV5 delta"  "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/delta.json"
probe "FIRST EPSS"       "https://api.first.org/data/v1/epss?limit=1"
probe "MITRE ATT&CK"     "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/index.json"
probe "ACSC alerts"      "https://www.cyber.gov.au/rss/alerts"
probe "CISA advisories"  "https://www.cisa.gov/cybersecurity-advisories/all.xml"

# ─── Secret hygiene ──────────────────────────────────────────────────────────
head_ "Secret hygiene"
git check-ignore -q .env && ok ".env is git-ignored" || bad ".env is NOT git-ignored — fix before committing"
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  bad ".env is TRACKED by git. Run: git rm --cached .env"
else
  ok ".env is not tracked by git"
fi
# Scan tracked files for real credential shapes. Exclude paths that legitimately
# contain example/fixture keys: the docs, this script, and the env template.
if git grep -nE '(sk-or-v1-|tvly-[A-Za-z0-9]{20}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22})' \
     -- . ':(exclude)docs/**' ':(exclude)ops/check-keys.sh' ':(exclude).env.example' >/dev/null 2>&1; then
  bad "a credential-shaped string was found in tracked files:"
  git grep -nE '(sk-or-v1-|tvly-[A-Za-z0-9]{20}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22})' \
     -- . ':(exclude)docs/**' ':(exclude)ops/check-keys.sh' ':(exclude).env.example' | sed 's/^/      /'
else
  ok "no credential patterns in tracked files (docs fixtures excluded)"
fi

printf '\n\033[1m%d passed, %d failed, %d skipped\033[0m\n' "$PASS" "$FAIL" "$SKIP"
[[ "$FAIL" -eq 0 ]] && echo "Ready for Stage 1." || echo "Fix the failures above, then re-run."
exit $(( FAIL > 0 ? 1 : 0 ))
