#
# Repository-specific settings for gwtests/Makefile
#

HWMODULENAME=GWCmdTemplate
BRIDGEBN=cmdtemplate
TOPNAME=wrapper
SRCDIR = $(abspath ../generated/$(HWMODULENAME))

TARGETS = cmd

.DEFAULT_GOAL := cmd

SCALA_SRCS := $(shell find ../src -name '*.scala') ../build.sbt
GENCMD = (cd ../ ; sbt "runMain cmdtemplate.GWCmdTemplate")
