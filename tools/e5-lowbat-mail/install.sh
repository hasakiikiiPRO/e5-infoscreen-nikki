#!/bin/sh
# Puts e5-lowbat-mail on the E5 (OpenWrt, USB LAN): the script, its cron line, and the SMTP
# account -- the password read from a file here (never in git, never on the command line).
#
#   tools/e5-lowbat-mail/install.sh PASSWORD_FILE [TO] [SMTP_USER]
#     TO and SMTP_USER default to 2465335064@qq.com (QQ mail: the password is an authorization code)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PW=$(cat "${1:?PASSWORD_FILE}")
TO=${2:-2465335064@qq.com}
USER=${3:-2465335064@qq.com}
E5=${E5:-root@192.168.9.1}
SSH="ssh -i $HOME/.ssh/e5_rec -o IdentitiesOnly=yes -o UserKnownHostsFile=$HOME/.config/e5rec/known_hosts -o HostKeyAlias=e5rec-e5 -o LogLevel=ERROR -o ConnectTimeout=8 $E5"
$SSH 'cat > /usr/sbin/e5-lowbat-mail && chmod 755 /usr/sbin/e5-lowbat-mail' < "$HERE/e5-lowbat-mail"
CA=$($SSH 'for f in /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem; do [ -f $f ] && echo $f && break; done')
printf 'account default\nhost smtp.qq.com\nport 465\ntls on\ntls_starttls off\ntls_trust_file %s\nauth on\nuser %s\npassword %s\nfrom %s\n#to %s\n' \
	"$CA" "$USER" "$PW" "$USER" "$TO" | $SSH 'umask 077; cat > /etc/e5-lowbat-msmtprc; chmod 600 /etc/e5-lowbat-msmtprc'
$SSH 'grep -q "^/etc/e5-lowbat-msmtprc$" /etc/sysupgrade.conf 2>/dev/null || echo /etc/e5-lowbat-msmtprc >> /etc/sysupgrade.conf
  (crontab -l 2>/dev/null | grep -v e5-lowbat-mail; echo "*/5 * * * * /usr/sbin/e5-lowbat-mail") | crontab -
  ls -la /usr/sbin/e5-lowbat-mail /etc/e5-lowbat-msmtprc; crontab -l | grep lowbat'
