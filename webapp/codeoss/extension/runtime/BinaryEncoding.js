/*
 * CINTERFORGE SOURCE HEADER — webapp/codeoss/extension/runtime/BinaryEncoding.js
 * PURPOSE: Provides the BinaryEncoding integration within the existing Code-OSS extension and authenticated Terminal host bridge.
 * OWNERSHIP: This authored source is part of one canonical Terminal runtime.
 * STARTUP: Called or loaded by the existing lifecycle/entry point; no
 *          hidden dependency provisioning or alternate VM is allowed.
 * SHUTDOWN: Release only resources explicitly owned by this component.
 * CALLERS: Preserve existing APIs, filenames, CLI flags and data formats.
 * KEY CLASSES / METHODS:
 *   encodeBase64: Encode encode base64 for this module.
 *   decodeBase64: Decode decode base64 for this module.
 * MAINTENANCE: Keep generated outputs in sibling Builds or the actual
 * Steam library; do not change Lagomorph timings, user assets or guest
 * readiness without tracing their real callers and failure boundaries.
*/
/* CinterForge Code - OSS classic front-end script. ASCII source only. */
(function (C) {
    'use strict';

    // Converts raw Uint8Array data to and from base64 for JSON control messages.
    // Filesystem payloads cross the host boundary as text-safe JSON, while callers
    // continue to work with byte arrays. Chunked encoding avoids oversized argument
    // lists when converting larger buffers to browser strings.

    // -----------------------------------------------------------------------------
    // Binary Encoding
    // -----------------------------------------------------------------------------

    // Encode Uint8Array-compatible bytes as base64. Process in bounded chunks because
    // spreading an entire large file into String.fromCharCode can exceed argument limits.
    function encodeBase64(bytes) {
        let binary = '';

        for (
            let offset = 0;
            offset < bytes.length;
            offset += 0x8000
        ) {
            binary += String.fromCharCode(
                ...bytes.subarray(
                    offset,
                    offset + 0x8000
                )
            );
        }

        return btoa(binary);
    }

    // Decode host-provided base64 into a fresh Uint8Array suitable for VS Code's
    // filesystem APIs. Empty input naturally produces an empty byte array.
    function decodeBase64(text) {
        const binary = atob(
            String(text || '')
        );

        const bytes = new Uint8Array(
            binary.length
        );

        for (
            let index = 0;
            index < binary.length;
            index++
        ) {
            bytes[index] = binary.charCodeAt(
                index
            );
        }

        return bytes;
    }

    C.encodeBase64 = encodeBase64;
    C.decodeBase64 = decodeBase64;
})(globalThis.CinterForgeCode);
