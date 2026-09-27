from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    ForgotPasswordView,
    LoginView,
    LogoutView,
    MyProfileView,
    OnlineStatusView,
    ProfileByExiIdView,
    ProfileSetupView,
    RegisterView,
    ResetPasswordView,
    SendOTPView,
    VerifyOTPView,
    UsernameCheckView,
    UsernameUpdateView,
    PhoneChangeRequestView,
    PhoneChangeVerifyView,
    ChangePhoneView,
    DeviceRegisterView,
    DeviceListView,
    DeviceRemoveView,
    LogoutOtherDevicesView,
    ChangePasswordView,
    PrivacySettingsView,
    BlockUserView,
    UnblockUserView,
    BlockedUserListView,
    DeactivateAccountView,
    DeleteAccountView,
    DeviceKeyUploadView,
    UserKeyBundleView,
    AdminUserListView,
    AdminUserDetailView,
    AdminUserBanView,
    AdminUserUnbanView,
    AdminUserForceLogoutView,
    AdminReportListView,
    AdminReportDetailView,
    AdminModerationOverviewView,
    AdminActivityLogListView,
    AdminAnalyticsView,
)
from .settings_views import (
    UnifiedSettingsView,
    NotificationSettingsView,
    NotificationMuteView,
    ChatPreferencesView,
    ChatBackupView,
    ChatRestoreView,
    ChatHistoryExportView,
    ChatHistoryClearAllView,
    ChatHistoryDeleteAllView,
    ChatArchiveAllView,
    ChatUnarchiveAllView,
    AppearanceSettingsView,
    StorageSettingsView,
    StorageUsageView,
    StorageChatsView,
    StorageLargeFilesView,
    StorageLargeFilesDeleteView,
    StorageClearMediaView,
)

urlpatterns = [
    # Login & Auth
    path("auth/login/", LoginView.as_view()),
    path("auth/logout/", LogoutView.as_view()),
    path("auth/forgot-password/", ForgotPasswordView.as_view()),
    path("auth/reset-password/", ResetPasswordView.as_view()),
    path("auth/token/refresh/", TokenRefreshView.as_view()),

    # OTP Verification
    path("auth/send-otp/", SendOTPView.as_view()),
    path("auth/verify-otp/", VerifyOTPView.as_view()),

    # Registration
    path("auth/register/", RegisterView.as_view()),

    # Profile setup
    path("profile/setup/", ProfileSetupView.as_view()),

    # Profile
    path("profile/me/", MyProfileView.as_view()),
    path("profile/status/", OnlineStatusView.as_view()),
    path("profile/<str:exi_id>/", ProfileByExiIdView.as_view()),

    # Account Section (Profile, Username, Phone Number, Linked Devices)
    path("account/profile/", MyProfileView.as_view()),
    path("account/username/", UsernameUpdateView.as_view()),
    path("account/username/check/", UsernameCheckView.as_view()),
    path("account/phone/", ChangePhoneView.as_view()),
    path("account/phone/request-change/", PhoneChangeRequestView.as_view()),
    path("account/phone/verify-change/", PhoneChangeVerifyView.as_view()),
    path("account/devices/", DeviceListView.as_view()),
    path("account/devices/register/", DeviceRegisterView.as_view()),
    path("account/devices/logout-others/", LogoutOtherDevicesView.as_view()),
    path("account/devices/<str:device_id>/", DeviceRemoveView.as_view()),

    # Linked Devices
    path("devices/register/", DeviceRegisterView.as_view()),
    path("devices/", DeviceListView.as_view()),
    path("devices/logout-others/", LogoutOtherDevicesView.as_view()),
    path("devices/<str:device_id>/", DeviceRemoveView.as_view()),

    # End-to-end encryption keys
    path("security/keys/", DeviceKeyUploadView.as_view()),
    path("security/keys/<str:exi_id>/", UserKeyBundleView.as_view()),

    # Privacy Section (Last Seen, Online Status, Read Receipts, Typing Status, Profile Photo, Blocked Users)
    path("security/privacy/", PrivacySettingsView.as_view()),
    path("privacy/", PrivacySettingsView.as_view()),
    path("account/privacy/", PrivacySettingsView.as_view()),

    # Blocked Users
    path("users/block/", BlockUserView.as_view()),
    path("users/unblock/", UnblockUserView.as_view()),
    path("users/<int:user_id>/block/", BlockUserView.as_view()),
    path("users/<int:user_id>/unblock/", UnblockUserView.as_view()),
    path("users/blocked/", BlockedUserListView.as_view()),
    path("privacy/blocked/", BlockedUserListView.as_view()),
    path("privacy/block/", BlockUserView.as_view()),
    path("privacy/block/<int:user_id>/", BlockUserView.as_view()),
    path("privacy/unblock/", UnblockUserView.as_view()),
    path("privacy/unblock/<int:user_id>/", UnblockUserView.as_view()),

    # Security
    path("security/change-password/", ChangePasswordView.as_view()),
    path("security/deactivate/", DeactivateAccountView.as_view()),
    path("security/delete-account/", DeleteAccountView.as_view()),

    # Module 10 — Admin panel (staff only)
    path("admin/users/", AdminUserListView.as_view()),
    path("admin/users/<int:user_id>/", AdminUserDetailView.as_view()),
    path("admin/users/<int:user_id>/ban/", AdminUserBanView.as_view()),
    path("admin/users/<int:user_id>/unban/", AdminUserUnbanView.as_view()),
    path("admin/users/<int:user_id>/force-logout/", AdminUserForceLogoutView.as_view()),
    path("admin/reports/", AdminReportListView.as_view()),
    path("admin/reports/<uuid:report_id>/", AdminReportDetailView.as_view()),
    path("admin/moderation/", AdminModerationOverviewView.as_view()),
    path("admin/activity-logs/", AdminActivityLogListView.as_view()),
    path("admin/analytics/", AdminAnalyticsView.as_view()),

    # Settings - Unified, Notifications, Chats, Appearance, Storage
    path("settings/", UnifiedSettingsView.as_view()),
    path("settings/all/", UnifiedSettingsView.as_view()),

    # Notifications Settings
    path("settings/notifications/", NotificationSettingsView.as_view()),
    path("settings/notifications/mute/", NotificationMuteView.as_view()),

    # Chats Settings
    path("settings/chats/", ChatPreferencesView.as_view()),
    path("settings/chats/backup/", ChatBackupView.as_view()),
    path("settings/chats/restore/", ChatRestoreView.as_view()),
    path("settings/chats/history/export/", ChatHistoryExportView.as_view()),
    path("settings/chats/history/clear-all/", ChatHistoryClearAllView.as_view()),
    path("settings/chats/history/delete-all/", ChatHistoryDeleteAllView.as_view()),
    path("settings/chats/archive-all/", ChatArchiveAllView.as_view()),
    path("settings/chats/unarchive-all/", ChatUnarchiveAllView.as_view()),

    # Appearance Settings
    path("settings/appearance/", AppearanceSettingsView.as_view()),

    # Storage Settings & Management
    path("settings/storage/", StorageSettingsView.as_view()),
    path("settings/storage/usage/", StorageUsageView.as_view()),
    path("settings/storage/chats/", StorageChatsView.as_view()),
    path("settings/storage/large-files/", StorageLargeFilesView.as_view()),
    path("settings/storage/large-files/delete/", StorageLargeFilesDeleteView.as_view()),
    path("settings/storage/clear-media/", StorageClearMediaView.as_view()),
]
