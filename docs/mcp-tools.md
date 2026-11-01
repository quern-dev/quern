# MCP tools

138 tools available via MCP. All tools are lazy-loaded and won't hog your context just by connecting the MCP. They are lightweight API wrappers and are easy for the Agent to use.

| Category | Tools |
|----------|-------|
| Server | `ensure_server`, `list_remotes`, `use_remote` |
| Updates | `update_quern`, `set_update_channel` |
| Logs | `tail_logs`, `query_logs`, `get_log_summary`, `get_errors`, `get_build_result`, `parse_build_output`, `get_latest_crash`, `clear_crashes`, `clear_device_crashes`, `set_log_filter`, `get_log_filter`, `list_log_sources`, `get_trace`, `start_recording`, `stop_recording`, `recording_keyframe`, `get_recording`, `list_recordings`, `download_recording`, `start_simulator_logging`, `stop_simulator_logging`, `start_device_logging`, `stop_device_logging`, `start_oslog_streaming`, `stop_oslog_streaming` |
| Network | `query_flows`, `list_pending_requests`, `wait_for_flow`, `get_flow_detail`, `get_flow_summary`, `start_capture_session`, `stop_capture_session`, `proxy_status`, `start_proxy`, `stop_proxy`, `proxy_setup_guide`, `verify_proxy_setup`, `install_proxy_cert`, `record_device_proxy_config`, `set_local_capture`, `add_session_capture`, `remove_session_capture`, `set_bypass`, `clear_bypass` |
| System Proxy | `configure_system_proxy`, `unconfigure_system_proxy` |
| Intercept & Mock | `set_intercept`, `clear_intercept`, `list_held_flows`, `release_flow`, `replay_flow`, `set_mock`, `list_mocks`, `update_mock`, `clear_mocks` |
| Device | `list_devices`, `boot_device`, `shutdown_device`, `erase_device`, `install_app`, `launch_app`, `terminate_app`, `uninstall_app`, `list_apps`, `get_performance`, `start_profile`, `stop_profile`, `get_profile`, `download_profile`, `build_and_install`, `record_android_build` |
| UI | `get_ui_tree`, `get_element_state`, `wait_for_element`, `get_screen_summary`, `tap`, `tap_element`, `swipe`, `gesture`, `scroll_to_element`, `type_text`, `clear_text`, `press_button`, `get_web_content`, `wait_for_settle`, `restore_simulator_input`, `set_hinge`, `get_hinge` |
| Screenshots | `take_screenshot`, `take_annotated_screenshot`, `start_screenshot_timeline`, `stop_screenshot_timeline`, `get_screenshot_timeline` |
| Device Config | `set_location`, `open_url`, `grant_permission`, `set_locale`, `set_hardware_keyboard`, `get_simulator_settings`, `set_simulator_setting`, `set_font_scale`, `set_display_density` |
| App State | `save_app_state`, `restore_app_state`, `list_app_states`, `delete_app_state` |
| Plist | `read_app_plist`, `set_app_plist_value`, `set_app_plist_values`, `diff_app_plist`, `delete_app_plist_key`, `start_plist_watch`, `stop_plist_watch`, `configure_plist_watch`, `get_plist_watch_config`, `unconfigure_plist_watch` |
| Device Pool | `resolve_device`, `ensure_devices` |
| Sessions | `reserve_devices`, `release_devices`, `session_status` |
| App Knowledge | `init_app_knowledge` |
| Landmarks | `load_landmarks`, `identify_screen`, `list_landmarks`, `unload_landmarks`, `validate_landmarks` |
| Preview | `preview_device`, `stop_preview`, `preview_status` |
| Physical Device | `setup_wda`, `start_driver`, `stop_driver` |

What each tool does, and the HTTP endpoint behind it, is in the [API reference](api-reference.md). `tests/test_readme_sync.py` checks this table and its count against the tools the MCP server registers.
