#!/usr/bin/env bash
# Keeps the newest KEEP database backups that scripts/deploy.sh made in DIR and removes the older ones.
#
#   scripts/prune-backups.sh DIR KEEP
#
# Only folders named like a deploy stamp (YYYY-MM-DD-HHMM or YYYY-MM-DD-HHMMSS) are counted or removed, never anything
# else in a shared place. They are ordered by their stamp (an HHMM stamp counts as HHMM00). Prints each folder it
# removes. Plain bash on purpose: GNU find's default regular expressions have no {n} counts, so a `find -regex` with
# them matched nothing and the backups piled up (15 of them, 5.1 GB, by 2026-11-15) while the run said nothing.
set -u

dir="${1:?usage: prune-backups.sh DIR KEEP}"
keep="${2:?usage: prune-backups.sh DIR KEEP}"
case "$keep" in ''|*[!0-9]*) echo "prune-backups.sh: KEEP must be a whole number, not '$keep'" >&2; exit 2 ;; esac
[ -d "$dir" ] || exit 0

stamps=()
for path in "$dir"/*; do
    [ -d "$path" ] && [ ! -L "$path" ] || continue
    name="${path##*/}"
    if [[ "$name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-([0-9]{4}|[0-9]{6})$ ]]; then
        key="$name"
        [ "${#BASH_REMATCH[1]}" = 4 ] && key="${name}00"
        stamps+=("$key $name")
    fi
done

count="${#stamps[@]}"
[ "$count" -gt "$keep" ] || exit 0
# oldest first; everything but the newest $keep goes
while IFS= read -r line; do
    name="${line#* }"
    rm -rf -- "${dir:?}/$name" && echo "removed $dir/$name"
done < <(printf '%s\n' "${stamps[@]}" | sort | head -n "$((count - keep))")
