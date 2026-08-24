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
    DeviceRegisterView,
    DeviceListView,
    DeviceRemoveView,
    LogoutOtherDevicesView,
    ChangePasswordView,
    PrivacySettingsView,
    BlockUserView,
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

urlpatterns = [
    # Login
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

    # Devices
    path("devices/register/", DeviceRegisterView.as_view()),
    path("devices/", DeviceListView.as_view()),
    path("devices/logout-others/", LogoutOtherDevicesView.as_view()),
    path("devices/<str:device_id>/", DeviceRemoveView.as_view()),

    # End-to-end encryption keys
    path("security/keys/", DeviceKeyUploadView.as_view()),
    path("security/keys/<str:exi_id>/", UserKeyBundleView.as_view()),

    # Security
    path("security/change-password/", ChangePasswordView.as_view()),
    path("security/privacy/", PrivacySettingsView.as_view()),
    path("users/<int:user_id>/block/", BlockUserView.as_view()),
    path("users/blocked/", BlockedUserListView.as_view()),
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
]
