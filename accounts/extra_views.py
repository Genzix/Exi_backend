"""
Extra Views for the Exi Backend
Covers:
  Security:   App Lock (PIN/Biometrics), Two-Step Verification,
              Security Notifications, Unknown Caller Protection
  Discovery:  Nearby Places, Businesses, Events, Offers, AI Recommendations
  Business:   Business Profile, Products/Services, Offers, Customer Chat
  Help:       Help Center, Report Problem, Contact Support, Terms, Privacy Policy
  Calls:      Call Notifications, Low Data Mode

No DB migrations required — all settings stored via user_preferences or Django cache.
"""
import hashlib
import secrets
import string
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .user_preferences import get_section_preferences, update_section_preferences

User = get_user_model()

# ============================================================
# SECURITY SECTION
# ============================================================

# ----------------------------------------
# App Lock — PIN / Biometrics
# ----------------------------------------

class AppLockSettingsView(APIView):
    """
    GET  /api/security/app-lock/  — Get App Lock status
    POST /api/security/app-lock/  — Enable/disable App Lock + set method
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "app_lock_enabled": prefs.get("app_lock_enabled", False),
            "app_lock_method": prefs.get("app_lock_method", "pin"),   # "pin" | "biometrics" | "both"
            "lock_after_seconds": prefs.get("lock_after_seconds", 60),
            "lock_on_background": prefs.get("lock_on_background", True),
            "pin_set": bool(prefs.get("app_lock_pin_hash")),
        })

    def post(self, request):
        data = request.data
        prefs = get_section_preferences(request.user.id, "security")

        enabled = data.get("app_lock_enabled")
        if enabled is not None:
            prefs["app_lock_enabled"] = bool(enabled)

        method = data.get("app_lock_method")
        if method is not None:
            if method not in ("pin", "biometrics", "both"):
                return Response(
                    {"detail": "app_lock_method must be 'pin', 'biometrics', or 'both'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            prefs["app_lock_method"] = method

        lock_after = data.get("lock_after_seconds")
        if lock_after is not None:
            try:
                prefs["lock_after_seconds"] = max(0, int(lock_after))
            except (TypeError, ValueError):
                return Response({"detail": "lock_after_seconds must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

        lock_bg = data.get("lock_on_background")
        if lock_bg is not None:
            prefs["lock_on_background"] = bool(lock_bg)

        update_section_preferences(request.user.id, "security", prefs)
        return Response({"detail": "App Lock settings updated.", **{
            k: prefs[k] for k in ("app_lock_enabled", "app_lock_method", "lock_after_seconds", "lock_on_background")
            if k in prefs
        }})


class AppLockPinSetView(APIView):
    """
    POST /api/security/app-lock/pin/
    Set or change the App Lock PIN (4–8 digits).
    Body: { pin: str, current_pin: str (required when changing) }
    """
    permission_classes = [permissions.IsAuthenticated]

    def _hash_pin(self, pin, user_id):
        return hashlib.sha256(f"{user_id}:{pin}".encode()).hexdigest()

    def post(self, request):
        pin = str(request.data.get("pin") or "").strip()
        if not pin.isdigit() or not (4 <= len(pin) <= 8):
            return Response({"detail": "PIN must be 4–8 digits."}, status=status.HTTP_400_BAD_REQUEST)

        prefs = get_section_preferences(request.user.id, "security")
        existing_hash = prefs.get("app_lock_pin_hash")

        if existing_hash:
            current_pin = str(request.data.get("current_pin") or "").strip()
            if not current_pin:
                return Response({"detail": "current_pin is required when changing PIN."}, status=status.HTTP_400_BAD_REQUEST)
            if self._hash_pin(current_pin, request.user.id) != existing_hash:
                return Response({"detail": "Incorrect current PIN."}, status=status.HTTP_400_BAD_REQUEST)

        prefs["app_lock_pin_hash"] = self._hash_pin(pin, request.user.id)
        prefs["app_lock_enabled"] = True
        prefs.setdefault("app_lock_method", "pin")
        update_section_preferences(request.user.id, "security", prefs)
        return Response({"detail": "App Lock PIN set successfully.", "app_lock_enabled": True})


class AppLockPinVerifyView(APIView):
    """
    POST /api/security/app-lock/verify/
    Verify the App Lock PIN.
    Body: { pin: str }
    """
    permission_classes = [permissions.IsAuthenticated]

    def _hash_pin(self, pin, user_id):
        return hashlib.sha256(f"{user_id}:{pin}".encode()).hexdigest()

    def post(self, request):
        pin = str(request.data.get("pin") or "").strip()
        prefs = get_section_preferences(request.user.id, "security")
        stored_hash = prefs.get("app_lock_pin_hash")

        if not stored_hash:
            return Response({"detail": "No PIN configured."}, status=status.HTTP_400_BAD_REQUEST)

        if self._hash_pin(pin, request.user.id) == stored_hash:
            return Response({"detail": "PIN verified.", "verified": True})
        return Response({"detail": "Incorrect PIN.", "verified": False}, status=status.HTTP_400_BAD_REQUEST)


# ----------------------------------------
# Two-Step Verification
# ----------------------------------------

class TwoStepVerificationView(APIView):
    """
    GET  /api/security/two-step/  — Get 2SV status
    POST /api/security/two-step/  — Enable/disable 2SV or set PIN
    """
    permission_classes = [permissions.IsAuthenticated]

    def _hash_pin(self, pin, user_id):
        return hashlib.sha256(f"2sv:{user_id}:{pin}".encode()).hexdigest()

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "two_step_enabled": prefs.get("two_step_enabled", False),
            "two_step_pin_set": bool(prefs.get("two_step_pin_hash")),
            "two_step_email": prefs.get("two_step_email", ""),
            "last_changed_at": prefs.get("two_step_last_changed_at"),
        })

    def post(self, request):
        action = request.data.get("action", "update")
        prefs = get_section_preferences(request.user.id, "security")

        if action == "enable":
            pin = str(request.data.get("pin") or "").strip()
            if not pin.isdigit() or not (4 <= len(pin) <= 8):
                return Response({"detail": "PIN must be 4–8 digits."}, status=status.HTTP_400_BAD_REQUEST)
            email = (request.data.get("email") or "").strip()
            prefs["two_step_pin_hash"] = self._hash_pin(pin, request.user.id)
            prefs["two_step_email"] = email[:254]
            prefs["two_step_enabled"] = True
            prefs["two_step_last_changed_at"] = timezone.now().isoformat()
            update_section_preferences(request.user.id, "security", prefs)
            return Response({"detail": "Two-step verification enabled.", "two_step_enabled": True})

        elif action == "disable":
            pin = str(request.data.get("pin") or "").strip()
            stored_hash = prefs.get("two_step_pin_hash")
            if stored_hash and self._hash_pin(pin, request.user.id) != stored_hash:
                return Response({"detail": "Incorrect PIN."}, status=status.HTTP_400_BAD_REQUEST)
            prefs["two_step_enabled"] = False
            update_section_preferences(request.user.id, "security", prefs)
            return Response({"detail": "Two-step verification disabled.", "two_step_enabled": False})

        elif action == "change_pin":
            current_pin = str(request.data.get("current_pin") or "").strip()
            new_pin = str(request.data.get("new_pin") or "").strip()
            stored_hash = prefs.get("two_step_pin_hash")

            if stored_hash and self._hash_pin(current_pin, request.user.id) != stored_hash:
                return Response({"detail": "Incorrect current PIN."}, status=status.HTTP_400_BAD_REQUEST)
            if not new_pin.isdigit() or not (4 <= len(new_pin) <= 8):
                return Response({"detail": "New PIN must be 4–8 digits."}, status=status.HTTP_400_BAD_REQUEST)

            prefs["two_step_pin_hash"] = self._hash_pin(new_pin, request.user.id)
            prefs["two_step_last_changed_at"] = timezone.now().isoformat()
            update_section_preferences(request.user.id, "security", prefs)
            return Response({"detail": "Two-step verification PIN changed."})

        return Response({"detail": "Invalid action."}, status=status.HTTP_400_BAD_REQUEST)


# ----------------------------------------
# Security Notifications
# ----------------------------------------

class SecurityNotificationsView(APIView):
    """
    GET  /api/security/notifications/  — Get security notification preferences
    PATCH /api/security/notifications/ — Update preferences
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "new_login_alert": prefs.get("new_login_alert", True),
            "password_change_alert": prefs.get("password_change_alert", True),
            "two_step_change_alert": prefs.get("two_step_change_alert", True),
            "suspicious_activity_alert": prefs.get("suspicious_activity_alert", True),
            "alert_email": prefs.get("alert_email", ""),
            "alert_push": prefs.get("alert_push", True),
        })

    def patch(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        bool_fields = ("new_login_alert", "password_change_alert", "two_step_change_alert",
                       "suspicious_activity_alert", "alert_push")
        for field in bool_fields:
            if field in request.data:
                prefs[field] = bool(request.data[field])

        if "alert_email" in request.data:
            prefs["alert_email"] = (request.data.get("alert_email") or "")[:254]

        update_section_preferences(request.user.id, "security", prefs)
        return Response({"detail": "Security notification settings updated.", **{
            k: prefs.get(k) for k in (*bool_fields, "alert_email")
        }})

    put = patch


# ----------------------------------------
# Unknown Caller Protection
# ----------------------------------------

class UnknownCallerProtectionView(APIView):
    """
    GET  /api/security/unknown-caller/  — Get setting
    POST /api/security/unknown-caller/  — Toggle
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        return Response({
            "silence_unknown_callers": prefs.get("silence_unknown_callers", False),
            "block_unknown_callers": prefs.get("block_unknown_callers", False),
        })

    def post(self, request):
        prefs = get_section_preferences(request.user.id, "security")
        if "silence_unknown_callers" in request.data:
            prefs["silence_unknown_callers"] = bool(request.data["silence_unknown_callers"])
        if "block_unknown_callers" in request.data:
            prefs["block_unknown_callers"] = bool(request.data["block_unknown_callers"])
        update_section_preferences(request.user.id, "security", prefs)
        return Response({
            "detail": "Unknown caller settings updated.",
            "silence_unknown_callers": prefs.get("silence_unknown_callers", False),
            "block_unknown_callers": prefs.get("block_unknown_callers", False),
        })


# ============================================================
# CALL SETTINGS SECTION (Notifications & Low Data Mode)
# ============================================================

class CallNotificationsView(APIView):
    """
    GET  /api/settings/calls/  — Get call notification settings
    PATCH /api/settings/calls/ — Update call notification settings
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        notif_prefs = get_section_preferences(request.user.id, "notifications")
        storage_prefs = get_section_preferences(request.user.id, "storage")
        return Response({
            "call_alerts": notif_prefs.get("call_alerts", True),
            "call_ringtone": notif_prefs.get("call_ringtone", "default"),
            "call_vibration": notif_prefs.get("call_vibration", "default"),
            "incoming_call_notification": notif_prefs.get("incoming_call_notification", True),
            "missed_call_notification": notif_prefs.get("missed_call_notification", True),
            "low_data_usage_calls": storage_prefs.get("low_data_usage_calls", False),
        })

    def patch(self, request):
        notif_data = {}
        storage_data = {}

        notif_fields = ("call_alerts", "incoming_call_notification", "missed_call_notification")
        for field in notif_fields:
            if field in request.data:
                notif_data[field] = bool(request.data[field])

        if "call_ringtone" in request.data:
            notif_data["call_ringtone"] = str(request.data["call_ringtone"])[:64]
        if "call_vibration" in request.data:
            val = request.data["call_vibration"]
            if val not in ("default", "short", "long", "off"):
                return Response(
                    {"detail": "call_vibration must be one of: default, short, long, off."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            notif_data["call_vibration"] = val

        if "low_data_usage_calls" in request.data:
            storage_data["low_data_usage_calls"] = bool(request.data["low_data_usage_calls"])

        if notif_data:
            update_section_preferences(request.user.id, "notifications", notif_data)
        if storage_data:
            update_section_preferences(request.user.id, "storage", storage_data)

        return Response({"detail": "Call settings updated."})

    put = patch


# ============================================================
# DISCOVERY SECTION (Nearby Places, Businesses, Events, Offers)
# ============================================================

# NOTE: Real implementations would integrate an external Places/Events API.
# These endpoints return structured responses that mirror what a production
# integration would return, so the client code is stable regardless.

class NearbyPlacesView(APIView):
    """
    GET /api/discovery/nearby/
    Discover nearby places. Query params: lat, lng, radius_km, category.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        lat = request.query_params.get("lat")
        lng = request.query_params.get("lng")
        radius_km = request.query_params.get("radius_km", "5")
        category = request.query_params.get("category", "all")

        if not lat or not lng:
            return Response(
                {"detail": "lat and lng query parameters are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            lat_f = float(lat)
            lng_f = float(lng)
            radius_f = min(float(radius_km), 50.0)
        except ValueError:
            return Response({"detail": "lat, lng, and radius_km must be numbers."}, status=status.HTTP_400_BAD_REQUEST)

        # Return a structured placeholder — in production connect to Google Places / HERE API
        return Response({
            "location": {"lat": lat_f, "lng": lng_f},
            "radius_km": radius_f,
            "category": category,
            "places": [],
            "total": 0,
            "note": "Connect your Places API key via PLACES_API_KEY setting to enable live results.",
        })


class NearbyBusinessesView(APIView):
    """
    GET /api/discovery/businesses/
    Discover nearby Exi-registered businesses.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        businesses_qs = User.objects.filter(
            is_business=True, is_active=True
        ).exclude(business_name="")

        if query:
            from django.db.models import Q
            businesses_qs = businesses_qs.filter(
                Q(business_name__icontains=query)
                | Q(display_name__icontains=query)
                | Q(bio__icontains=query)
            )

        businesses = []
        for biz in businesses_qs[:50]:
            businesses.append({
                "user_id": biz.id,
                "exi_id": biz.exi_id,
                "business_name": biz.business_name,
                "display_name": biz.display_name or biz.business_name,
                "bio": biz.bio,
                "business_address": biz.business_address,
                "business_website": biz.business_website,
                "profile_photo": request.build_absolute_uri(biz.profile_photo.url) if biz.profile_photo else None,
            })

        return Response({
            "query": query,
            "count": len(businesses),
            "businesses": businesses,
        })


class DiscoveryEventsView(APIView):
    """
    GET /api/discovery/events/
    List upcoming events. Stored in user_preferences keyed per-user (demo).
    In production, events would be a separate DB table.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # In production: query Events model
        # For now: return structured empty response
        category = request.query_params.get("category", "all")
        return Response({
            "category": category,
            "events": [],
            "total": 0,
            "note": "Events feature — integrate with events DB table for live results.",
        })


class DiscoveryOffersView(APIView):
    """
    GET /api/discovery/offers/
    List active offers/promotions from businesses.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # In production: query Offer model
        return Response({
            "offers": [],
            "total": 0,
            "note": "Offers feature — integrate with offers DB table for live results.",
        })


class AIRecommendationsView(APIView):
    """
    GET /api/discovery/ai-recommendations/
    AI-powered recommendations based on user activity.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # In production: call ML recommendation service
        return Response({
            "recommendations": {
                "suggested_contacts": [],
                "trending_businesses": [],
                "nearby_events": [],
                "curated_offers": [],
            },
            "generated_at": timezone.now().isoformat(),
            "note": "AI Recommendations — connect to recommendation engine for live results.",
        })


# ============================================================
# BUSINESS SECTION
# ============================================================

class BusinessProfileView(APIView):
    """
    GET  /api/business/profile/  — Get business profile
    PATCH /api/business/profile/ — Update business profile
    POST /api/business/setup/    — Set up as business account
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        prefs = get_section_preferences(user.id, "business")
        return Response({
            "is_business": user.is_business,
            "business_name": user.business_name,
            "business_address": user.business_address,
            "business_website": user.business_website,
            "bio": user.bio,
            "display_name": user.display_name,
            "profile_photo": request.build_absolute_uri(user.profile_photo.url) if user.profile_photo else None,
            "exi_id": user.exi_id,
            "categories": prefs.get("categories", []),
            "working_hours": prefs.get("working_hours", {}),
            "social_links": prefs.get("social_links", {}),
            "verified": prefs.get("verified", False),
            "rating": prefs.get("rating", None),
            "review_count": prefs.get("review_count", 0),
        })

    def patch(self, request):
        user = request.user
        update_fields = []

        if "business_name" in request.data:
            user.business_name = (request.data["business_name"] or "")[:100]
            update_fields.append("business_name")
        if "business_address" in request.data:
            user.business_address = (request.data["business_address"] or "")[:255]
            update_fields.append("business_address")
        if "business_website" in request.data:
            user.business_website = (request.data["business_website"] or "")[:200]
            update_fields.append("business_website")
        if "bio" in request.data:
            user.bio = (request.data["bio"] or "")[:160]
            update_fields.append("bio")
        if "display_name" in request.data:
            user.display_name = (request.data["display_name"] or "")[:50]
            update_fields.append("display_name")

        if update_fields:
            user.save(update_fields=update_fields)

        # Extended prefs
        prefs = get_section_preferences(user.id, "business")
        if "categories" in request.data:
            prefs["categories"] = [str(c)[:50] for c in (request.data["categories"] or [])][:10]
        if "working_hours" in request.data and isinstance(request.data["working_hours"], dict):
            prefs["working_hours"] = request.data["working_hours"]
        if "social_links" in request.data and isinstance(request.data["social_links"], dict):
            prefs["social_links"] = request.data["social_links"]
        update_section_preferences(user.id, "business", prefs)

        return Response({"detail": "Business profile updated."})

    put = patch


class BusinessSetupView(APIView):
    """
    POST /api/business/setup/
    Convert a personal account to a business account.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        business_name = (request.data.get("business_name") or "").strip()
        if not business_name:
            return Response({"detail": "business_name is required."}, status=status.HTTP_400_BAD_REQUEST)

        user.is_business = True
        user.business_name = business_name[:100]
        if request.data.get("business_address"):
            user.business_address = str(request.data["business_address"])[:255]
        if request.data.get("business_website"):
            user.business_website = str(request.data["business_website"])[:200]
        user.save(update_fields=["is_business", "business_name", "business_address", "business_website"])

        return Response({
            "detail": "Business account set up successfully.",
            "is_business": True,
            "business_name": user.business_name,
        })


class BusinessProductsView(APIView):
    """
    GET  /api/business/products/  — List products/services
    POST /api/business/products/  — Add product/service
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        return Response({
            "products": prefs.get("products", []),
            "total": len(prefs.get("products", [])),
        })

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "Product/service name is required."}, status=status.HTTP_400_BAD_REQUEST)

        prefs = get_section_preferences(request.user.id, "business")
        products = prefs.get("products", [])

        import uuid
        product = {
            "id": str(uuid.uuid4()),
            "name": name[:100],
            "description": (request.data.get("description") or "")[:500],
            "price": str(request.data.get("price") or "")[:50],
            "currency": (request.data.get("currency") or "INR")[:10],
            "image_url": (request.data.get("image_url") or "")[:512],
            "is_available": bool(request.data.get("is_available", True)),
            "created_at": timezone.now().isoformat(),
        }
        products.append(product)
        prefs["products"] = products
        update_section_preferences(request.user.id, "business", prefs)
        return Response(product, status=status.HTTP_201_CREATED)


class BusinessProductDetailView(APIView):
    """
    GET    /api/business/products/<product_id>/  — Get product
    PATCH  /api/business/products/<product_id>/  — Update product
    DELETE /api/business/products/<product_id>/  — Delete product
    """
    permission_classes = [permissions.IsAuthenticated]

    def _find(self, products, product_id):
        for i, p in enumerate(products):
            if p.get("id") == product_id:
                return i, p
        return None, None

    def get(self, request, product_id):
        prefs = get_section_preferences(request.user.id, "business")
        _, product = self._find(prefs.get("products", []), product_id)
        if product is None:
            return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(product)

    def patch(self, request, product_id):
        prefs = get_section_preferences(request.user.id, "business")
        products = prefs.get("products", [])
        idx, product = self._find(products, product_id)
        if product is None:
            return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)

        for field in ("name", "description", "price", "currency", "image_url"):
            if field in request.data:
                product[field] = str(request.data[field] or "")[:{"name": 100, "description": 500, "price": 50, "currency": 10, "image_url": 512}.get(field, 255)]
        if "is_available" in request.data:
            product["is_available"] = bool(request.data["is_available"])

        products[idx] = product
        prefs["products"] = products
        update_section_preferences(request.user.id, "business", prefs)
        return Response(product)

    def delete(self, request, product_id):
        prefs = get_section_preferences(request.user.id, "business")
        products = prefs.get("products", [])
        idx, _ = self._find(products, product_id)
        if idx is None:
            return Response({"detail": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        products.pop(idx)
        prefs["products"] = products
        update_section_preferences(request.user.id, "business", prefs)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BusinessOffersView(APIView):
    """
    GET  /api/business/offers/  — List offers
    POST /api/business/offers/  — Create offer
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        offers = prefs.get("offers", [])
        # Filter expired
        now = timezone.now().isoformat()
        active = [o for o in offers if not o.get("expires_at") or o["expires_at"] > now]
        return Response({"offers": active, "total": len(active)})

    def post(self, request):
        title = (request.data.get("title") or "").strip()
        if not title:
            return Response({"detail": "Offer title is required."}, status=status.HTTP_400_BAD_REQUEST)

        prefs = get_section_preferences(request.user.id, "business")
        offers = prefs.get("offers", [])

        import uuid
        offer = {
            "id": str(uuid.uuid4()),
            "title": title[:100],
            "description": (request.data.get("description") or "")[:500],
            "discount_percent": request.data.get("discount_percent"),
            "discount_amount": request.data.get("discount_amount"),
            "promo_code": (request.data.get("promo_code") or "")[:30],
            "valid_from": (request.data.get("valid_from") or timezone.now().isoformat()),
            "expires_at": request.data.get("expires_at"),
            "created_at": timezone.now().isoformat(),
        }
        offers.append(offer)
        prefs["offers"] = offers
        update_section_preferences(request.user.id, "business", prefs)
        return Response(offer, status=status.HTTP_201_CREATED)


class BusinessCustomerChatView(APIView):
    """
    GET /api/business/customer-chat/
    Get customer chat settings — e.g., greeting message, away message,
    business hours, auto-reply.
    PATCH /api/business/customer-chat/ — Update settings
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        return Response({
            "greeting_message": prefs.get("greeting_message", ""),
            "away_message": prefs.get("away_message", ""),
            "auto_reply_enabled": prefs.get("auto_reply_enabled", False),
            "auto_reply_message": prefs.get("auto_reply_message", ""),
            "response_time_label": prefs.get("response_time_label", "Usually responds within a few hours"),
            "allow_messages_from_non_contacts": prefs.get("allow_messages_from_non_contacts", True),
        })

    def patch(self, request):
        prefs = get_section_preferences(request.user.id, "business")
        text_fields = {
            "greeting_message": 500,
            "away_message": 500,
            "auto_reply_message": 500,
            "response_time_label": 100,
        }
        for field, maxlen in text_fields.items():
            if field in request.data:
                prefs[field] = (request.data[field] or "")[:maxlen]
        if "auto_reply_enabled" in request.data:
            prefs["auto_reply_enabled"] = bool(request.data["auto_reply_enabled"])
        if "allow_messages_from_non_contacts" in request.data:
            prefs["allow_messages_from_non_contacts"] = bool(request.data["allow_messages_from_non_contacts"])

        update_section_preferences(request.user.id, "business", prefs)
        return Response({"detail": "Customer chat settings updated."})

    put = patch


# ============================================================
# HELP SECTION
# ============================================================

HELP_ARTICLES = [
    {"id": "getting-started", "title": "Getting Started with EXI", "category": "basics",
     "summary": "Learn how to set up your profile, find contacts, and start chatting."},
    {"id": "privacy-controls", "title": "Privacy Controls", "category": "privacy",
     "summary": "Control who can see your last seen, profile photo, and online status."},
    {"id": "two-step-verification", "title": "Two-Step Verification", "category": "security",
     "summary": "Add an extra layer of security to your account with a PIN."},
    {"id": "group-chats", "title": "Creating and Managing Group Chats", "category": "chats",
     "summary": "Learn how to create groups, add members, and manage admin controls."},
    {"id": "calls", "title": "Voice & Video Calls", "category": "calls",
     "summary": "Make high-quality voice and video calls with your contacts."},
    {"id": "media-storage", "title": "Managing Media & Storage", "category": "storage",
     "summary": "Clear old media, manage auto-downloads, and free up storage."},
    {"id": "business-profile", "title": "Setting Up a Business Profile", "category": "business",
     "summary": "Switch to a business account and manage your products and offers."},
    {"id": "blocked-contacts", "title": "Blocking Contacts", "category": "privacy",
     "summary": "Block unwanted contacts and manage your blocked list."},
    {"id": "backup-restore", "title": "Chat Backup & Restore", "category": "chats",
     "summary": "Back up your chats and restore them on a new device."},
    {"id": "disappearing-messages", "title": "Disappearing Messages", "category": "chats",
     "summary": "Set messages to automatically disappear after a set time."},
]

FAQ_ITEMS = [
    {"question": "How do I change my phone number?",
     "answer": "Go to Account > Phone Number > Request Change and follow the verification steps."},
    {"question": "Can I use EXI on multiple devices?",
     "answer": "Yes! You can link up to 5 devices under Account > Linked Devices."},
    {"question": "How do I delete my account?",
     "answer": "Go to Settings > Security > Delete Account. This action is permanent."},
    {"question": "Why are my messages not delivering?",
     "answer": "Check your internet connection. The recipient may have blocked you or turned off notifications."},
    {"question": "How do I report spam?",
     "answer": "Open the chat, tap the three-dot menu, and select 'Report'. You can also block the sender."},
]

TERMS_TEXT = """TERMS OF SERVICE

Last updated: October 2026

1. ACCEPTANCE — By using EXI, you agree to these Terms.
2. ELIGIBILITY — You must be 13 years or older to use EXI.
3. PRIVACY — We respect your privacy. See our Privacy Policy.
4. PROHIBITED USE — Do not use EXI for illegal, harmful, or abusive activities.
5. CONTENT — You own your content. We do not sell it.
6. TERMINATION — We may suspend accounts that violate these Terms.
7. LIMITATION OF LIABILITY — EXI is provided \"as is\" without warranties.
8. GOVERNING LAW — These Terms are governed by applicable law.

For the full Terms of Service, visit our website.
"""

PRIVACY_POLICY_TEXT = """PRIVACY POLICY

Last updated: October 2026

WHAT WE COLLECT
- Account info (email/phone, name, profile photo)
- Device identifiers for push notifications
- Messages (end-to-end encrypted, we cannot read them)
- Usage data (anonymised analytics)

HOW WE USE IT
- To provide and improve the EXI service
- To send service notifications
- For security and fraud prevention

WHAT WE DON'T DO
- We do not sell your personal data
- We do not read your messages
- We do not share data with advertisers

YOUR RIGHTS
- Access, correct, or delete your data at any time
- Export your chat history under Settings > Chats
- Delete your account under Settings > Security

For the full Privacy Policy, visit our website.
"""


class HelpCenterView(APIView):
    """
    GET /api/help/  — Help Center home (articles + FAQ)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query = (request.query_params.get("q") or "").strip().lower()
        category = (request.query_params.get("category") or "").strip().lower()

        articles = HELP_ARTICLES
        if query:
            articles = [a for a in articles if query in a["title"].lower() or query in a["summary"].lower()]
        if category:
            articles = [a for a in articles if a["category"] == category]

        faq = FAQ_ITEMS
        if query:
            faq = [f for f in faq if query in f["question"].lower() or query in f["answer"].lower()]

        categories = list({a["category"] for a in HELP_ARTICLES})

        return Response({
            "categories": sorted(categories),
            "articles": articles,
            "faq": faq if not category else [],
            "total_articles": len(articles),
        })


class HelpArticleView(APIView):
    """
    GET /api/help/articles/<article_id>/  — Get full article
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, article_id):
        article = next((a for a in HELP_ARTICLES if a["id"] == article_id), None)
        if not article:
            return Response({"detail": "Article not found."}, status=status.HTTP_404_NOT_FOUND)
        # In production load full content from DB / CMS
        return Response({**article, "content": article["summary"] + "\n\nFull content coming soon."})


class ReportProblemView(APIView):
    """
    POST /api/help/report-problem/
    Submit a problem report.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        category = (request.data.get("category") or "other").strip()
        description = (request.data.get("description") or "").strip()
        if not description:
            return Response({"detail": "description is required."}, status=status.HTTP_400_BAD_REQUEST)

        # Store in cache for demo; production would write to DB or Zendesk
        import uuid
        ticket_id = f"EXI-{str(uuid.uuid4())[:8].upper()}"
        cache.set(f"problem_report:{ticket_id}", {
            "ticket_id": ticket_id,
            "user_id": request.user.id,
            "category": category,
            "description": description[:2000],
            "submitted_at": timezone.now().isoformat(),
            "status": "submitted",
        }, timeout=86400 * 30)

        return Response({
            "detail": "Problem report submitted successfully. Our team will review it.",
            "ticket_id": ticket_id,
            "status": "submitted",
        }, status=status.HTTP_201_CREATED)


class ContactSupportView(APIView):
    """
    GET  /api/help/contact/  — Get support contact options
    POST /api/help/contact/  — Send a support message
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({
            "support_options": [
                {"type": "chat", "label": "Chat with Support", "available": True,
                 "description": "Get help from our support team in real time."},
                {"type": "email", "label": "Email Support", "available": True,
                 "email": "support@exi.app",
                 "description": "Send us an email and we'll respond within 24 hours."},
                {"type": "faq", "label": "Browse FAQ", "available": True,
                 "url": "/api/help/", "description": "Find answers to common questions."},
            ],
            "business_hours": "Monday–Friday, 9 AM–6 PM IST",
            "average_response_time": "Within 24 hours",
        })

    def post(self, request):
        subject = (request.data.get("subject") or "").strip()
        message = (request.data.get("message") or "").strip()
        if not message:
            return Response({"detail": "message is required."}, status=status.HTTP_400_BAD_REQUEST)

        import uuid
        ticket_id = f"EXI-{str(uuid.uuid4())[:8].upper()}"
        cache.set(f"support_ticket:{ticket_id}", {
            "ticket_id": ticket_id,
            "user_id": request.user.id,
            "subject": subject[:200],
            "message": message[:2000],
            "submitted_at": timezone.now().isoformat(),
            "status": "open",
        }, timeout=86400 * 30)

        return Response({
            "detail": "Support message sent. We'll get back to you within 24 hours.",
            "ticket_id": ticket_id,
            "status": "open",
        }, status=status.HTTP_201_CREATED)


class TermsView(APIView):
    """
    GET /api/help/terms/  — Get Terms of Service
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({
            "title": "Terms of Service",
            "last_updated": "2026-10-01",
            "version": "1.0",
            "content": TERMS_TEXT,
            "url": "https://exi.app/terms",
        })


class PrivacyPolicyView(APIView):
    """
    GET /api/help/privacy/  — Get Privacy Policy
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({
            "title": "Privacy Policy",
            "last_updated": "2026-10-01",
            "version": "1.0",
            "content": PRIVACY_POLICY_TEXT,
            "url": "https://exi.app/privacy",
        })
