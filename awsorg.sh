awsorg() {
  if [ "$#" -eq 0 ]; then
    echo "Usage: awsorg <aws-cli-command>"
    echo "Example: awsorg ec2 describe-vpcs"
    echo "Example: awsorg s3api list-buckets"
    return 1
  fi

  command -v aws >/dev/null || {
    echo "Error: AWS CLI is required"
    return 1
  }

  local login_profile="${AWS_PROFILE:-default}"

  # Make sure the active profile is an SSO profile.
  if ! aws configure get sso_session --profile "$login_profile" >/dev/null 2>&1 &&
     ! aws configure get sso_start_url --profile "$login_profile" >/dev/null 2>&1; then
    echo "Error: active profile '$login_profile' does not appear to be an AWS SSO profile."
    echo "Set AWS_PROFILE to one of your SSO profiles."
    return 1
  fi

  # Make sure we are already logged in via SSO.
  aws sts get-caller-identity \
    --profile "$login_profile" \
    --region us-gov-west-1 \
    >/dev/null 2>&1 || {
      echo "Error: not logged in via AWS SSO for profile '$login_profile'."
      echo "Run: aws sso login --profile $login_profile"
      return 1
    }

  local rc=0
  local profile account_id region cmd_rc

  while read -r profile; do
    [ -z "$profile" ] && continue

    # Only use SSO profiles.
    if ! aws configure get sso_session --profile "$profile" >/dev/null 2>&1 &&
       ! aws configure get sso_start_url --profile "$profile" >/dev/null 2>&1; then
      continue
    fi

    account_id="$(
      aws sts get-caller-identity \
        --profile "$profile" \
        --region us-gov-west-1 \
        --query 'Account' \
        --output text 2>/dev/null
    )"

    if [ $? -ne 0 ] || [ -z "$account_id" ]; then
      echo "Skipping profile '$profile': unable to use SSO credentials" >&2
      rc=1
      continue
    fi

    for region in us-gov-east-1 us-gov-west-1; do
      echo "===== profile=${profile}  account=${account_id}  region=${region} =====" >&2

      aws --profile "$profile" \
        --region "$region" \
        --no-cli-pager \
        "$@"

      cmd_rc=$?
      [ $cmd_rc -ne 0 ] && rc=$cmd_rc
    done
  done < <(aws configure list-profiles)

  return "$rc"
}

all-s3-buckets() {
  while read -r profile; do
    [ -z "$profile" ] && continue

    if ! aws configure get sso_session --profile "$profile" >/dev/null 2>&1 &&
       ! aws configure get sso_start_url --profile "$profile" >/dev/null 2>&1; then
      continue
    fi

    echo "===== profile=${profile} =====" >&2

    aws --profile "$profile" \
      --region us-gov-west-1 \
      --no-cli-pager \
      s3api list-buckets \
      --query 'Buckets[].Name' \
      --output text
  done
}
