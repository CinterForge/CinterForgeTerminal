"""Small, deliberately maintained lifecycle regression gate.

This module is the *only* Python suite executed by ``./Cinter test``.
The real Setup/Build/Stage/Test acceptance checks are still run by Cinter;
these checks focus on observable contracts and expensive regressions rather
than tightly mocking each internal helper. Legacy diagnostic cases remain
available for explicit developer investigation, but do not gate releases.

To add a check here, replace a redundant check rather than growing the gate.
"""

import unittest


# Selected from existing test modules; no duplicate implementation or fixtures.
# Fully qualified names are intentionally explicit: a renamed/deleted check
# fails discovery rather than silently dropping its acceptance contract.
CONTRACT_TESTS = (
    # Lifecycle phase separation and durable logs
    'test_suite_lifecycle_logs.LifecycleLogTests.test_setup_preflight_on_current_authored_tree',
    'test_suite_lifecycle_logs.LifecycleLogTests.test_failed_stage_writes_full_log_and_machine_readable_result',
    'test_suite_setup.SetupPreflightOrderingTests.test_test_all_refuses_missing_stage_receipts',
    'test_suite_setup.SetupPreflightOrderingTests.test_missing_tool_fails_setup_before_expensive_build',
    # Four artifact build, stage, release, and integrity
    'test_suite_release.PublishContractTests.test_missing_target_rejects_publish_all',
    'test_suite_release.PublishContractTests.test_dry_run_never_resolves_credentials_or_makes_network_request',
    'test_suite_release.PublishContractTests.test_tampered_stage_or_unlisted_file_fails_closed',
    'test_suite_release.FourArtifactSteamReleaseTests.test_build_four_unpacked_and_stage_all_without_zip',
    'test_suite_release.FourArtifactSteamReleaseTests.test_build_status_and_stage_status_are_separate',
    'test_suite_release.FourArtifactSteamReleaseTests.test_stage_rejects_modified_build_input_before_install',
    'test_suite_release.FourArtifactSteamReleaseTests.test_stage_does_not_delete_unowned_installed_files',
    'test_suite_release.FourArtifactSteamReleaseTests.test_same_size_rewrite_even_if_mtime_restored_invalidates_build',
    'test_suite_release.FourArtifactSteamReleaseTests.test_mac_stage_preserves_old_app_on_failed_verification',
    'test_suite_release.FourArtifactSteamReleaseTests.test_published_zip_archives_contain_no_python_caches',
    # Explicit publish channels and public/private isolation
    'test_suite_download_readme.PublishChannelTests.test_beta_publishes_only_to_beta_prefix_and_never_adds_lagomorph',
    'test_suite_download_readme.PublishChannelTests.test_combined_lifecycle_never_uploads_without_named_channel',
    'test_suite_download_readme.PublishChannelTests.test_publish_wrapper_dryrun_never_requests_apple_notarization',
    'test_suite_cdn_config.ManagedCloudflareConfigTests.test_cloudflare_config_never_switches_private_to_public_bucket',
    # Native MSI packaging, cache correctness, and atomic failure handling
    'test_suite_msi.MSIAtomicPublishContracts.test_msi_cache_fingerprint_ignores_file_timestamps_and_modes',
    'test_suite_msi.MSIAtomicPublishContracts.test_native_mac_publish_does_not_start_guest_and_reuses_msi',
    'test_suite_msi.MSIAtomicPublishContracts.test_native_receipt_requires_staged_tool_digests',
    'test_suite_msi.MSIAtomicPublishContracts.test_publish_compiles_once_then_reuses_verified_msi_without_docker',
    'test_suite_msi.MSIAtomicPublishContracts.test_rebuilt_same_bytes_new_mtimes_reuses_verified_installer',
    'test_suite_msi.MSIAtomicPublishContracts.test_rebuild_failure_preserves_prior_msi_receipt_and_discards_scratch',
    'test_suite_msi.MSIAtomicPublishContracts.test_corrupt_distribution_msi_is_not_reused',
    # Apple Developer ID, Hardened Runtime, notarization and DMG
    'test_suite_xcode_signing.XcodeSigningTests.test_xcode_project_selects_macos_specific_release_signer',
    'test_suite_xcode_signing.XcodeSigningTests.test_normal_release_does_not_use_diagnostic_beta_exceptions',
    'test_suite_xcode_signing.XcodeSigningTests.test_codesign_uses_real_login_home_not_colima_managed_home',
    'test_suite_xcode_signing.NotaryProfileTests.test_noninteractive_missing_notary_refuses_before_any_enrollment',
    'test_suite_mac_dmg.DragToApplicationsDMGTests.test_notary_receipt_required_for_public_dmg',
    'test_suite_mac_dmg.DragToApplicationsDMGTests.test_mounted_dmg_always_detaches_on_error',
    # Real platform identity and owned guest isolation
    'test_suite_windows.WindowsNativeAcceptanceTests.test_refuses_wine_linux_and_macos_as_windows_acceptance',
    'test_suite_amd64_backend.Amd64ProducerTests.test_cross_image_refuses_arm64_linuxkit_c',
    'test_suite_amd64_backend.Amd64ProducerTests.test_cross_image_accepts_qemu_x86_64_guest',
    'test_suite_guest_connection_contract.PersistentConnectionContractTests.test_receipt_is_test_only_evidence_not_an_implicit_setup',
    # Bedrock world safety and reconnectable console
    'test_suite_bedrock.BedrockWorldDiscoveryCommandTests.test_run_uses_readonly_source_copy_and_reports_port',
    'test_suite_bedrock.BedrockWorldDiscoveryCommandTests.test_unsafe_symlinked_source_is_rejected',
    'test_suite_bedrock.MinecraftConsoleInteractiveModeRegressionTests.test_console_forwards_commands_until_exit_without_stopping_server',
    'test_suite_bedrock.ReconnectTests.test_automatic_run_does_not_restart_or_power_off_old_vm',
    # Game catalog, IF save data and original Classic Mac resources
    'test_suite_games.WarGamesLaunchTests.test_first_game_of_exact_68_is_unchanged_joshua',
    'test_suite_games.MacGamesAndHiddenLagomorphTests.test_uploaded_library_catalog_has_67_unique_commands',
    'test_suite_games.ExternalIFTitleTests.test_manifest_is_exact_16_and_zork_command_names',
    'test_suite_games.IFSteamAutosaveTests.test_host_autosave_roundtrip_previous_and_story_identity',
    'test_suite_lagomorph_cloudflare.LagomorphCloudflareTests.test_classic_mac_forks_survive_private_zip_and_reapply_when_data_unchanged',
    'test_suite_lagomorph_cloudflare.LagomorphCloudflareTests.test_pull_rejects_snapshot_path_traversal_without_mutating_builds',
    # Terminal access and security
    'test_suite_shell_server_runtime.ShellServerRuntimeTests.test_setpass_enforces_browser_login_including_localhost',
    'test_suite_shell_server_runtime.ShellServerRuntimeTests.test_native_http_parent_exit_releases_its_own_listener',
    'test_suite_shell_server_runtime.ShellServerRuntimeTests.test_codeoss_local_workspace_persists_and_serves_real_files',
    'test_suite_ui.CommandHelpTests.test_help_covers_exact_supported_operations',
    # Steam library selection / installed-app filesystem isolation
    'test_suite_steam_libraries.SteamLibraryDiscoveryTests.test_readonly_discovery_does_not_create_install_folder',
    'test_suite_steamapps_scratch.SteamScratchIsolationTests.test_cache_cannot_redirect_through_steam_symlink',
    # Persistent Code-OSS setup logging
    'test_suite_codeoss_progress.CodeOSSSetupProgressTests.test_long_command_streams_output_and_reports_pass',
)


def load_tests(loader, tests, pattern):
    """Select the fixed 52-contract gate, not all imported legacy methods."""
    suite = unittest.TestSuite()
    for name in CONTRACT_TESTS:
        suite.addTests(loader.loadTestsFromName(name))
    return suite
