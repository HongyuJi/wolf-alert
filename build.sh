#!/bin/bash

docker build --tag wolf-alert .
docker run -d --rm wolf-alert