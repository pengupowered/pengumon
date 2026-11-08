# Sourced by scripts/test.sh and scripts/test-ui.sh. Builds a test image so that each run leaves no orphan behind.
#
# Every build re-points a tag and orphans the image it pointed to (a "dangling" image: a few hundred MB of layers per
# source change, for ever). So the tag is per work tree (this checkout's path), not shared by all of them, the image
# carries a label with the same key, and after a successful build the work tree's previous images are removed.
# Only images with this work tree's label are touched, so a build in another work tree running at the same time is
# never affected. An image still used by a container refuses to go ("in use"): that error is ignored. Layers shared with
# the new image stay, so the layer cache works as before.
#
# build_test_image <name> <root> <dockerfile> [extra build args...]  sets $built to the new image id.
build_test_image() {
    local name="$1" root="$2" dockerfile="$3" key iid id new
    shift 3
    key="$(printf %s "$root" | sha1sum | cut -c1-12)"
    iid="$(mktemp)"
    # APT_REFRESH: the date, so the apt-get upgrade layer is rebuilt once a day (Dockerfile)
    podman build -q --iidfile "$iid" --label "pengumon.test-tree=$key" --build-arg "APT_REFRESH=$(date -u +%Y-%m-%d)" "$@" \
        -f "$root/$dockerfile" \
        -t "localhost/$name:$key" "$root" >/dev/null
    built="$(cat "$iid")"; rm -f "$iid"
    new="${built#sha256:}"
    for id in $(podman images --filter "label=pengumon.test-tree=$key" --format '{{.ID}}' --no-trunc 2>/dev/null); do
        [ "${id#sha256:}" = "$new" ] || podman rmi "$id" >/dev/null 2>&1 || true
    done
}
