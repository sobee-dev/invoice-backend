from rest_framework import serializers

from .models import PushToken, Notification


class RegisterTokenSerializer(serializers.Serializer):
    """
    Registers (or re-attaches) an Expo push token to the current user.
    If the token already exists — same device, different account
    logged in since — it's reassigned rather than duplicated, since
    the unique constraint on `token` means only one user can own a
    given device token at a time.
    """
    expo_push_token = serializers.CharField(max_length=255)
    platform = serializers.ChoiceField(
        choices=PushToken.PLATFORM_CHOICES, required=False, allow_blank=True
    )

    def save(self):
        user = self.context['request'].user
        token = self.validated_data['expo_push_token']
        platform = self.validated_data.get('platform', '')

        obj, _ = PushToken.objects.update_or_create(
            token=token,
            defaults={'user': user, 'platform': platform},
        )
        return obj


class UnregisterTokenSerializer(serializers.Serializer):
    """Removes a token — called on logout so a signed-out device stops
    receiving pushes meant for the account it just left."""
    expo_push_token = serializers.CharField(max_length=255)

    def validate_expo_push_token(self, value):
        user = self.context['request'].user
        if not PushToken.objects.filter(token=value, user=user).exists():
            raise serializers.ValidationError("Token not found for this user.")
        return value

    def save(self):
        user = self.context['request'].user
        token = self.validated_data['expo_push_token']
        PushToken.objects.filter(token=token, user=user).delete()


class NotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'notification_type', 'title', 'body', 'data',
            'is_read', 'read_at', 'created_at',
        ]
        read_only_fields = fields