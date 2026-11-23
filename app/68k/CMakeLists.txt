# CinterForgeTerminal: real classic Macintosh 68K APPL (not a native host app).
# Build uses the Setup-provisioned, sealed Retro68 OCI in existing managed Docker.
cmake_minimum_required(VERSION 3.9)
project(CinterForgeTerminal68K C)
add_application(CinterForgeTerminal CinterForgeTerminal.c generated_icons.r memory.r
    TYPE "APPL" CREATOR "CFTM")
