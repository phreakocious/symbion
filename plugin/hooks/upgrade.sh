# The detached upgrade worker: `sh upgrade.sh PV`, launched by a shim with a full
# path, stdout and stderr on /dev/null, and hold-PV already made (its exclusive
# create is the lock). It decides from `symbion -V` alone, closes PV with ok-PV,
# and always exits 0: a hook never fails a session. POSIX sh. State is
# $CLAUDE_PLUGIN_DATA/upgrade/.
exec >/dev/null 2>&1
# The shims' cut (session_start.sh says why), though the lib launches this with a `/`.
here=.
case $0 in */*) here=${0%/*} ;; *\\*) here=${0%\\*} ;; esac
. "$here/upgrade_lib.sh"

# True when A >= B, each reduced to its leading X.Y.Z: a .devN, rcN or +local
# suffix is dropped, as setup's check drops it, and a missing field reads 0.
symbion_version_ge() {
  awk -v a="$1" -v b="$2" '
    function f(s, o,  n) { match(s, /^[0-9]+(\.[0-9]+)*/); n = split(substr(s, 1, RLENGTH), o, "."); for (; n < 3; ) o[++n] = 0 }
    BEGIN { f(a, x); f(b, y); for (i = 1; i <= 3; i++) { if (x[i] + 0 > y[i] + 0) exit 0; if (x[i] + 0 < y[i] + 0) exit 1 } exit 0 }'
}

# Prints uv, pipx, editable or other for LOC, the path `symbion -V` gives. Outside
# site-packages and dist-packages (Debian's system Python) the package is a source
# tree (editable); inside, the venv is LOC cut at its last /lib/ (or \Lib\ on
# Windows) and its marker file names the tool. A system install has none: other.
symbion_install_kind() {
  case $1 in *site-packages* | *dist-packages*) ;; *) echo editable; return 0 ;; esac
  _v=${1%/lib/*}
  [ "$_v" != "$1" ] || _v=${1%\\Lib\\*}
  if [ -f "$_v/uv-receipt.toml" ]; then echo uv
  elif [ -f "$_v/pipx_metadata.json" ]; then echo pipx
  else echo other; fi
}

# Appends one attempt to log.jsonl, from started pv before after kind cmd rc out.
symbion_log() {
  _t=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  _o=$(printf '%s\n' "$out" | symbion_json_text)
  _b=$(printf '%s' "$before" | symbion_json_text) _a=$(printf '%s' "$after" | symbion_json_text)
  _c=$(printf '%s' "$cmd" | symbion_json_text)
  printf '{"time":"%s","started":"%s","plugin":"%s","before":"%s","after":"%s","kind":"%s","command":"%s","exit":%s,"outcome":"%s","output":"%s"}\n' \
    "$_t" "$started" "$pv" "$_b" "$_a" "$kind" "$_c" "${rc:-null}" "$1" "$_o" >> "$_up/log.jsonl"
}

# Writes the agent's one line through a temp name, so a reader never sees half of it.
symbion_result() {
  printf '%s\n' "$1" | symbion_json_text > "$_up/result.tmp.$$" && mv "$_up/result.tmp.$$" "$_up/result"
}

# Logs OUTCOME, then closes pv: ok-PV in, hold-PV out.
symbion_close() {
  symbion_log "$1"
  : > "$_up/ok-$pv"
  rm -f "$_up/hold-$pv"
}

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
pv=$1
case $pv in ''|*[!0-9A-Za-z.+-]*) exit 0 ;; esac
[ -n "$CLAUDE_PLUGIN_DATA" ] || exit 0
_up=$CLAUDE_PLUGIN_DATA/upgrade
_cr=$(printf '\r')
before= after= kind= cmd= rc= out=

# stderr stays out: a warning there (PYTHONWARNINGS) would read as the version.
_v=$(symbion -V 2>/dev/null) || { symbion_close none; exit 0; }
# Every use below tolerates a CR as well; the strip keeps a future path test from tripping on one.
_v=${_v%"$_cr"}
# "symbion VERSION from LOC (git SHA)": VERSION and LOC, each cut to its own field.
# Only " (git " ends LOC: a path may hold " (" itself. Quoted: ksh (OpenBSD's sh)
# cannot parse a bare "(" there, and the worker died silently.
_v=${_v#symbion }
before=${_v%% *}
_loc=${_v#* from }; _loc=${_loc%%' (git '*}
after=$before
if symbion_version_ge "$before" "$pv"; then
  # A waiting line that symbion needs a version it now has is stale: the user
  # upgraded by hand after a failed or refused one (audit, 2026-11-12).
  _r=; { IFS= read -r _r < "$_up/result"; } 2>/dev/null
  case $_r in *'needs symbion '*)
    _n=${_r#*needs symbion }; _n=${_n%% *}
    symbion_version_ge "$before" "$_n" && rm -f "$_up/result" ;;
  esac
  symbion_close current; exit 0
fi

kind=$(symbion_install_kind "$_loc")
case $kind in
  editable | other)
    # `symbion -V` reads the version the install recorded, which a pull leaves.
    if [ "$kind" = editable ]; then _how='Pull its checkout and run `pip install -e .` there again to upgrade it.'
    else _what='not from uv tool or pipx' _how='Upgrade it with the tool that installed it.'; fi
    symbion_result "symbion: the symbion plugin needs symbion $pv or later, and this one is $before, an install the plugin does not upgrade (${_what:-$kind} at $_loc). $_how Tell the user."
    symbion_close notice
    exit 0 ;;
esac

case $kind in uv) cmd='uv tool upgrade symbion' ;; *) cmd='pipx upgrade symbion' ;; esac
# Windows has no serve restart: a running serve holds the package's files, so the user stops it.
# (The texts sit in variables: an apostrophe inside "${v:+...}" is not portable.)
case $(uname -s) in
  MINGW* | MSYS* | CYGWIN*)
    _win=1
    _wf=" A running \`symbion serve\` can hold symbion's files: stop it first."
    _wu=" Restart any running \`symbion serve\` by hand: Ctrl-C it and start it again." ;;
  *) _win= _wf= _wu= ;;
esac
# A missing tool is the shell's own 127, with its "not found" line in out.
out=$($cmd 2>&1 < /dev/null); rc=$?
_bad=$rc
# Read even after a failure, for the log: uv on Windows replaced the package, then
# failed to copy the command a running serve held (live run, 2026-11-12).
_a=$(symbion -V 2>/dev/null); _a=${_a%"$_cr"}; _a=${_a#symbion }
after=${_a%% *}
if [ "$rc" -eq 0 ]; then
  # A CLI the upgrade left with no version is broken, not waiting for PyPI: as a
  # wait it would retry each hour and never be reported.
  case $after in [0-9]*) ;; *) _bad=1; out="$out
symbion -V gave no version after the upgrade" ;; esac
fi

if [ "$_bad" -ne 0 ]; then
  # hold-PV stays: its mtime throttles the retry. told-PV keeps the report to once per version.
  symbion_log failed
  if [ ! -e "$_up/told-$pv" ]; then
    : > "$_up/told-$pv"
    symbion_result "symbion: the symbion plugin needs symbion $pv or later, and \`$cmd\` failed (log: $_up/log.jsonl). Tell the user, who can run it by hand.$_wf"
  fi
  exit 0
fi

# PyPI may not list the plugin's version yet: no ok marker, so a later session
# retries. wait-PV dates the first wait. A wait past a day is told once (told-PV):
# `pipx upgrade` drops an install's index args and `uv tool upgrade` keeps a pin,
# and either waits forever.
if ! symbion_version_ge "$after" "$pv"; then
  symbion_log wait
  if [ ! -e "$_up/wait-$pv" ]; then : > "$_up/wait-$pv"
  elif [ ! -e "$_up/told-$pv" ] && [ -n "$(find "$_up/wait-$pv" -prune -mmin +1440 2>/dev/null)" ]; then
    : > "$_up/told-$pv"
    symbion_result "symbion: the symbion plugin needs symbion $pv or later, and a day of \`$cmd\` has left it at $before (log: $_up/log.jsonl). The install may pin a version or a package index. Tell the user, who can upgrade it by hand."
  fi
  exit 0
fi

symbion_result "symbion: the symbion CLI was upgraded from $before to $after to match the symbion plugin. Tell the user. If the symbion skill was loaded before this line, run \`symbion skill\` before the next symbion write.$_wu"
symbion_close upgraded
if [ -z "$_win" ]; then
  cmd='symbion serve --restart'
  out=$(symbion serve --restart 2>&1 < /dev/null); rc=$?
  symbion_log restart
fi
exit 0
