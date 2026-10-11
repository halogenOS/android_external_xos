export ROM_REVISION="XOS-16.2"

# Forgejo service hosting the XOS repositories. Override any of these to point
# the tools at a different instance, organization or SSH endpoint.
export XOS_FORGEJO_URL="${XOS_FORGEJO_URL:-https://git.halogenos.org}"
export XOS_FORGEJO_OWNER="${XOS_FORGEJO_OWNER:-halogenOS}"
export XOS_FORGEJO_SSH_BASE="${XOS_FORGEJO_SSH_BASE:-git@git.halogenos.org}"

function getPlatformPath() {
  PWD="$(pwd)"
  original_string="$PWD"
  string_to_replace="$(realpath $(gettop))"
  result_string="${original_string//$string_to_replace}"
  echo -n "$result_string"
}

function getNonPrefixedUnderscorePath() {
  ppath="$(getPlatformPath)"
  echo -n "${ppath//\//_}"
}

function getUnderscorePath() {
  local prefix="android"
  if [ $# -ge 1 ]; then
    local prefix="$1"
  fi
  echo -n "${prefix}$(getNonPrefixedUnderscorePath)"
}

function getForgejoToken() {
  if [ -n "$XOS_FORGEJO_TOKEN" ]; then
    echo -n "$XOS_FORGEJO_TOKEN"
    return 0
  fi
  if [ -r "$HOME/.creds/xos_forgejo_token" ]; then
    tr -d '\r\n' < "$HOME/.creds/xos_forgejo_token"
    return 0
  fi
  return 1
}

createXos() {
  local project_to_create="$1"
  if [ -z "$project_to_create" ]; then
    local project_to_create="$(getUnderscorePath)"
  fi
  local token
  if ! token="$(getForgejoToken)" || [ -z "$token" ]; then
    echo "No Forgejo token found (set XOS_FORGEJO_TOKEN or create ~/.creds/xos_forgejo_token)" >&2
    return 1
  fi
  # Build the request body with a real JSON encoder so repository names never
  # have to be hand-escaped.
  local payload
  payload="$(python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1], "private": False, "auto_init": False}))' "$project_to_create")" || return 1
  echo "Creating $project_to_create"
  # curl succeeds on any HTTP answer, so ask for the status code and decide
  # here: only a 2xx creation and the 409 of an already existing repository
  # count as success, everything else (bad token, no permission, server
  # failure) has to fail loudly.
  local response http_code body
  response="$(curl -sS -w '\n%{http_code}' \
    -H "Authorization: token $token" \
    -H "Content-Type: application/json" \
    -X POST \
    --data "$payload" \
    "$XOS_FORGEJO_URL/api/v1/orgs/$XOS_FORGEJO_OWNER/repos")" || return 1
  http_code="${response##*$'\n'}"
  body="${response%$'\n'*}"
  case "$http_code" in
    2??)
      echo "Created $project_to_create"
      ;;
    409)
      echo "Repository $project_to_create already exists"
      ;;
    *)
      echo "Failed to create $project_to_create (HTTP $http_code): $body" >&2
      return 1
      ;;
  esac
}

function addXos() {
  local xos_url="$XOS_FORGEJO_SSH_BASE:$XOS_FORGEJO_OWNER/$(getUnderscorePath)"
  if git remote get-url xos >/dev/null 2>/dev/null; then
    git remote set-url xos "$xos_url"
  else
    git remote add xos "$xos_url"
  fi
  git fetch xos
}

function addXosGithub() {
  if  git remote get-url xosgh >/dev/null 2>/dev/null; then
    git remote set-url xosgh git@github.com:halogenOS/$(getUnderscorePath)
  else
    git remote add xosgh git@github.com:halogenOS/$(getUnderscorePath)
  fi
  git fetch xosgh
}

function addAosp() {
  git remote remove aosp 2>/dev/null
  git remote add aosp https://android.googlesource.com/platform/$(getPlatformPath).git
  git fetch aosp
}

function addLOS() {
  git remote remove los 2>/dev/null
  local remote_domain="github.com"
  if [ $# -ge 1 ]; then
	  remote_domain="$1"
  fi
  local usp=$(getUnderscorePath)
  usp=${usp/build_make/build}
  git remote add los https://$remote_domain/LineageOS/${usp}.git
  git fetch los
}

function unLFS() {
  git lfs install --local || :
  git lfs fetch
  git lfs checkout
  lfs_files=$(git lfs ls-files | awk '{ print $3 }')
  if [ -n "$lfs_files" ]; then
    echo $lfs_files | xargs -I% git rm --cached %
    echo $lfs_files | xargs -I% git lfs untrack %
  fi
  rm -f .gitattributes .lfsconfig
  git add .gitattributes .lfsconfig
  git commit -m "Un-LFS"
  git lfs uninstall --local
  if [ -n "$lfs_files" ]; then
    echo $lfs_files | xargs -I% git add %
  fi
  git add -A
  git commit -m "Directly checkout LFS files"
}
