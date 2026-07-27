#!/usr/bin/env bash

set -e

supervisorctl status sssd
supervisorctl status cupsd

exit 0
