from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from chats.models import (
    Conversation,
    ConversationParticipant,
    Media,
    Message,
    StarredMessage,
)

User = get_user_model()


class StorageFeaturesTestCase(APITestCase):
    def setUp(self):
        # Create users
        self.user = User.objects.create_user(
            username="test_user",
            email="test_user@example.com",
            password="Password123!",
            display_name="Test User",
        )
        self.other_user = User.objects.create_user(
            username="other_user",
            email="other_user@example.com",
            password="Password123!",
            display_name="Other User",
        )

        # JWT auth
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        # Create direct conversation
        self.conversation = Conversation.objects.create(type="direct")
        self.p1 = ConversationParticipant.objects.create(
            conversation=self.conversation,
            user=self.user,
        )
        self.p2 = ConversationParticipant.objects.create(
            conversation=self.conversation,
            user=self.other_user,
        )

    def test_10_1_storage_usage(self):
        """10.1 Storage Usage: returns categorized breakdown (Photos, Videos, Documents, Audio, Total)."""
        # Create a photo message with attachment
        photo_file = SimpleUploadedFile("photo.jpg", b"x" * (2 * 1024 * 1024), content_type="image/jpeg")
        Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            message_type="image",
            attachment=photo_file,
            attachment_name="photo.jpg",
            attachment_size=2 * 1024 * 1024,
        )

        # Create a video message with attachment
        video_file = SimpleUploadedFile("video.mp4", b"x" * (5 * 1024 * 1024), content_type="video/mp4")
        Message.objects.create(
            conversation=self.conversation,
            sender=self.other_user,
            message_type="video",
            attachment=video_file,
            attachment_name="video.mp4",
            attachment_size=5 * 1024 * 1024,
        )

        # Create a document message
        doc_file = SimpleUploadedFile("doc.pdf", b"x" * (1 * 1024 * 1024), content_type="application/pdf")
        Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            message_type="document",
            attachment=doc_file,
            attachment_name="doc.pdf",
            attachment_size=1 * 1024 * 1024,
        )

        # Create an audio message
        audio_file = SimpleUploadedFile("audio.mp3", b"x" * (512 * 1024), content_type="audio/mpeg")
        Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            message_type="audio",
            attachment=audio_file,
            attachment_name="audio.mp3",
            attachment_size=512 * 1024,
        )

        response = self.client.get("/api/settings/storage/usage/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertIn("storage_usage", data)
        usage = data["storage_usage"]

        self.assertIn("photos", usage)
        self.assertIn("videos", usage)
        self.assertIn("documents", usage)
        self.assertIn("audio", usage)
        self.assertIn("total", usage)

        self.assertEqual(usage["photos"]["count"], 1)
        self.assertEqual(usage["videos"]["count"], 1)
        self.assertEqual(usage["documents"]["count"], 1)
        self.assertEqual(usage["audio"]["count"], 1)
        self.assertEqual(usage["total"]["count"], 4)

        # Verify formatted strings
        self.assertIn("MB", usage["photos"]["formatted"])
        self.assertIn("MB", usage["videos"]["formatted"])
        self.assertIn("MB", usage["documents"]["formatted"])
        self.assertIn("KB", usage["audio"]["formatted"])
        self.assertIn("MB", usage["total"]["formatted"])

    def test_10_2_manage_storage_overview_and_batch_delete(self):
        """10.2 Manage Storage: overview categories and batch delete preserving message."""
        # Create a large file (> 5MB)
        large_msg = Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            content="Check this large video",
            message_type="video",
            attachment=SimpleUploadedFile("large_video.mp4", b"x" * (6 * 1024 * 1024), content_type="video/mp4"),
            attachment_name="large_video.mp4",
            attachment_size=6 * 1024 * 1024,
        )

        # Check overview
        response = self.client.get("/api/settings/storage/manage/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("categories", data)
        categories = data["categories"]
        self.assertIn("large_files", categories)
        self.assertIn("frequently_forwarded", categories)
        self.assertIn("old_media", categories)
        self.assertIn("unused_media", categories)
        self.assertGreaterEqual(categories["large_files"]["count"], 1)

        # Check category listing
        cat_resp = self.client.get("/api/settings/storage/manage/category/?category=large_files&sort=largest")
        self.assertEqual(cat_resp.status_code, status.HTTP_200_OK)
        cat_data = cat_resp.json()
        self.assertGreaterEqual(cat_data["total_count"], 1)

        # Batch delete with message retention (delete_message=False)
        del_resp = self.client.post("/api/settings/storage/manage/delete/", {
            "item_ids": [str(large_msg.id)],
            "delete_message": False,
        })
        self.assertEqual(del_resp.status_code, status.HTTP_200_OK)
        del_data = del_resp.json()
        self.assertEqual(del_data["deleted_count"], 1)
        self.assertEqual(del_data["messages_retained"], 1)
        self.assertEqual(del_data["messages_deleted"], 0)

        # Verify the Message row STILL EXISTS in the database
        large_msg.refresh_from_db()
        self.assertEqual(large_msg.content, "Check this large video")
        self.assertFalse(bool(large_msg.attachment))
        self.assertEqual(large_msg.attachment_size, 0)

    def test_10_3_large_files_sorting(self):
        """10.3 Large Files: lists large files and sorts by largest, newest, oldest."""
        now = timezone.now()

        # File 1: 35 MB, oldest
        m1 = Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            message_type="video",
            attachment=SimpleUploadedFile("movie.mp4", b"x" * 100, content_type="video/mp4"),
            attachment_name="movie.mp4",
            attachment_size=35 * 1024 * 1024,
        )
        Message.objects.filter(id=m1.id).update(created_at=now - timedelta(days=5))

        # File 2: 85 MB, medium
        m2 = Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            message_type="video",
            attachment=SimpleUploadedFile("Video.mp4", b"x" * 100, content_type="video/mp4"),
            attachment_name="Video.mp4",
            attachment_size=85 * 1024 * 1024,
        )
        Message.objects.filter(id=m2.id).update(created_at=now - timedelta(days=2))

        # File 3: 42 MB, newest
        m3 = Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            message_type="document",
            attachment=SimpleUploadedFile("Presentation.pdf", b"x" * 100, content_type="application/pdf"),
            attachment_name="Presentation.pdf",
            attachment_size=42 * 1024 * 1024,
        )
        Message.objects.filter(id=m3.id).update(created_at=now - timedelta(days=1))

        # Test Sort by Largest
        resp_largest = self.client.get("/api/settings/storage/large-files/?sort=largest")
        self.assertEqual(resp_largest.status_code, status.HTTP_200_OK)
        files = resp_largest.json()["files"]
        self.assertEqual(files[0]["file_name"], "Video.mp4")
        self.assertEqual(files[0]["formatted_size"], "85 MB")
        self.assertEqual(files[1]["file_name"], "Presentation.pdf")
        self.assertEqual(files[1]["formatted_size"], "42 MB")
        self.assertEqual(files[2]["file_name"], "movie.mp4")
        self.assertEqual(files[2]["formatted_size"], "35 MB")

        # Test Sort by Newest
        resp_newest = self.client.get("/api/settings/storage/large-files/?sort=newest")
        self.assertEqual(resp_newest.status_code, status.HTTP_200_OK)
        files_newest = resp_newest.json()["files"]
        self.assertEqual(files_newest[0]["file_name"], "Presentation.pdf")

        # Test Sort by Oldest
        resp_oldest = self.client.get("/api/settings/storage/large-files/?sort=oldest")
        self.assertEqual(resp_oldest.status_code, status.HTTP_200_OK)
        files_oldest = resp_oldest.json()["files"]
        self.assertEqual(files_oldest[0]["file_name"], "movie.mp4")

    def test_10_4_auto_download_settings(self):
        """10.4 Auto Download: configure mobile data, Wi-Fi, and Roaming."""
        # Get settings
        response = self.client.get("/api/settings/storage/auto-download/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["settings"]
        self.assertIn("mobile_data", data)
        self.assertIn("wifi", data)
        self.assertIn("roaming", data)

        # Update valid settings (matching user's example: Mobile Data = Photos + Audio)
        patch_resp = self.client.patch("/api/settings/storage/auto-download/", {
            "mobile_data": ["photos", "audio"],
            "wifi": ["photos", "videos", "documents", "audio"],
            "roaming": [],
            "low_data_usage_calls": True,
        })
        self.assertEqual(patch_resp.status_code, status.HTTP_200_OK)
        updated = patch_resp.json()["settings"]
        self.assertEqual(updated["mobile_data"], ["photos", "audio"])
        self.assertEqual(updated["roaming"], [])
        self.assertTrue(updated["low_data_usage_calls"])

        # Test invalid media category validation
        invalid_resp = self.client.patch("/api/settings/storage/auto-download/", {
            "mobile_data": ["invalid_type"],
        })
        self.assertEqual(invalid_resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_10_5_clear_media_with_message_retention_and_starred_protection(self):
        """10.5 Clear Media: selective clearing, message retention, and starred message protection."""
        # Photo 1: normal photo
        photo1 = Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            content="Regular photo",
            message_type="image",
            attachment=SimpleUploadedFile("regular.jpg", b"x" * 1024, content_type="image/jpeg"),
            attachment_name="regular.jpg",
            attachment_size=1024,
        )

        # Photo 2: starred photo
        photo2 = Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            content="Favorite photo",
            message_type="image",
            attachment=SimpleUploadedFile("favorite.jpg", b"x" * 2048, content_type="image/jpeg"),
            attachment_name="favorite.jpg",
            attachment_size=2048,
        )
        StarredMessage.objects.create(user=self.user, message=photo2)

        # Video 1: video
        video1 = Message.objects.create(
            conversation=self.conversation,
            sender=self.user,
            content="Video clip",
            message_type="video",
            attachment=SimpleUploadedFile("clip.mp4", b"x" * 4096, content_type="video/mp4"),
            attachment_name="clip.mp4",
            attachment_size=4096,
        )

        # Test Clear Media Preview (dry-run)
        preview_resp = self.client.post("/api/settings/storage/clear-media/preview/", {
            "clear_photos": True,
            "clear_videos": False,
            "keep_starred": True,
            "delete_message": False,
        })
        self.assertEqual(preview_resp.status_code, status.HTTP_200_OK)
        preview_data = preview_resp.json()
        self.assertEqual(preview_data["deleted_files_count"], 1)  # only photo1, photo2 is protected
        self.assertEqual(preview_data["starred_protected_count"], 1)

        # Execute Clear Media (only clear photos, keep starred, preserve message)
        clear_resp = self.client.post("/api/settings/storage/clear-media/", {
            "clear_photos": True,
            "clear_videos": False,
            "keep_starred": True,
            "delete_message": False,
        })
        self.assertEqual(clear_resp.status_code, status.HTTP_200_OK)
        clear_data = clear_resp.json()
        self.assertEqual(clear_data["deleted_files_count"], 1)
        self.assertEqual(clear_data["messages_retained"], 1)

        # Verify photo1 message row is retained but attachment is cleared
        photo1.refresh_from_db()
        self.assertEqual(photo1.content, "Regular photo")
        self.assertFalse(bool(photo1.attachment))
        self.assertEqual(photo1.attachment_size, 0)

        # Verify photo2 (starred) is completely intact with its attachment
        photo2.refresh_from_db()
        self.assertTrue(bool(photo2.attachment))
        self.assertEqual(photo2.attachment_size, 2048)

        # Verify video1 is untouched
        video1.refresh_from_db()
        self.assertTrue(bool(video1.attachment))
        self.assertEqual(video1.attachment_size, 4096)
