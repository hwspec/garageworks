#!/usr/bin/env bash
# Publishes the current -SNAPSHOT version to Sonatype Central's snapshot repo.
#
# Bypasses `sbt publishSigned`'s upload step: sbt/Ivy's plain URL resolver only
# authenticates via HTTP challenge-response, which central.sonatype.com (behind
# Cloudflare) doesn't trigger correctly, so every upload attempt 401s regardless
# of credentials. curl sends Basic auth preemptively and works fine, so this
# script builds+signs with sbt, then uploads with curl.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION=$(grep -oE 'ThisBuild / version *:= *"[^"]*"' build.sbt | sed -E 's/.*"([^"]*)"/\1/')
if [[ "$VERSION" != *-SNAPSHOT ]]; then
  echo "version in build.sbt is '$VERSION', not a -SNAPSHOT version. Aborting." >&2
  exit 1
fi

ARTIFACT_ID="garageworks_2.13"
GROUP_PATH="io/github/hwspec"
CRED_FILE="$HOME/.sbt/1.0/sonatype.sbt"

echo "Building artifacts for $ARTIFACT_ID-$VERSION ..."
sbt -no-colors package packageSrc packageDoc makePom

DIR="target/scala-2.13"
FILES=(
  "$ARTIFACT_ID-$VERSION.jar"
  "$ARTIFACT_ID-$VERSION-sources.jar"
  "$ARTIFACT_ID-$VERSION-javadoc.jar"
  "$ARTIFACT_ID-$VERSION.pom"
)

echo "Signing artifacts with GPG ..."
for f in "${FILES[@]}"; do
  gpg --batch --yes -a -b "$DIR/$f"
done

LINE=$(grep -oE 'Credentials\("[^"]*", *"[^"]*", *"[^"]*", *"[^"]*"\)' "$CRED_FILE")
USER=$(echo "$LINE" | sed -E 's/Credentials\("[^"]*", *"[^"]*", *"([^"]*)".*/\1/')
PASS=$(echo "$LINE" | sed -E 's/.*"([^"]*)"\)$/\1/')

BASEURL="https://central.sonatype.com/repository/maven-snapshots/$GROUP_PATH/$ARTIFACT_ID/$VERSION"

echo "Uploading to $BASEURL ..."
for f in "${FILES[@]}" "${FILES[@]/%/.asc}"; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" -u "$USER:$PASS" -X PUT -T "$DIR/$f" "$BASEURL/$f")
  echo "$f -> HTTP $STATUS"
  if [[ "$STATUS" != "201" && "$STATUS" != "200" ]]; then
    echo "Upload failed for $f (HTTP $STATUS)" >&2
    exit 1
  fi
done

echo "Done. Published $ARTIFACT_ID:$VERSION to Sonatype Central snapshots."
