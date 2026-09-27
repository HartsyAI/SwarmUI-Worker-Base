#!/usr/bin/env bash
# Builds SwarmUI (and, for BACKEND=hartsyinference, the HartsyInference extension) at pinned commits.
# Runs in the .NET SDK stage; the runtime image only ever receives the build output.
set -euo pipefail

: "${SWARMUI_REPO:?}" "${SWARMUI_REF:?}" "${BACKEND:?}"

# Checks out an exact commit. `git clone --depth 1` cannot target an arbitrary sha, so fetch it.
fetch_ref() {
    local dest="$1" repo="$2" ref="$3"
    git init -q "$dest"
    git -C "$dest" remote add origin "$repo"
    git -C "$dest" fetch -q --depth 1 origin "$ref"
    git -C "$dest" checkout -q FETCH_HEAD
}

fetch_ref /opt/swarmui "$SWARMUI_REPO" "$SWARMUI_REF"
cd /opt/swarmui
dotnet build src/SwarmUI.csproj --configuration Release -o ./src/bin/live_release
git rev-parse HEAD > src/bin/last_build

case "$BACKEND" in
    comfyui)
        ;;
    hartsyinference)
        : "${HARTSYINFERENCE_REPO:?}" "${HARTSYINFERENCE_REF:?}"
        ext=src/Extensions/SwarmUI-HartsyInference
        fetch_ref "$ext" "$HARTSYINFERENCE_REPO" "$HARTSYINFERENCE_REF"
        # Reproduce SwarmUI's own extension build exactly (Core/ExtensionsManager.cs BuildExtension):
        # it skips building only when this precise output file exists, named after the extension's
        # git HEAD. Getting the name wrong means every cold worker rebuilds, and the runtime image has
        # no SDK to rebuild with.
        hash="$(git -C "$ext" rev-parse HEAD | cut -c1-8)"
        dll=SwarmExtensionSwarmUI-HartsyInference
        dotnet build "$(ls "$ext"/*.csproj)" -c Release -o "/opt/swarmui/src/bin/extensions/$dll/" \
            "-p:BaseIntermediateOutputPath=/opt/swarmui/src/obj/extensions/$dll/;TargetName=$dll-$hash"
        test -f "/opt/swarmui/src/bin/extensions/$dll/$dll-$hash.dll"
        ;;
    *)
        echo "Unknown BACKEND '$BACKEND' (expected comfyui or hartsyinference)" >&2
        exit 1
        ;;
esac

# Intermediate build files are never needed at runtime.
rm -rf /opt/swarmui/src/obj
