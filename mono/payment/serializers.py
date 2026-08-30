from rest_framework import serializers
from .models import Payment

class VerifySerializer(serializers.Serializer):
    authority = serializers.CharField()

class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ("id","status","authority","ref_id","amount","currency","zarinpal_code","zarinpal_message","target_type","target_id")


class StartPaymentSerializer(serializers.Serializer):
    authority = serializers.CharField()

    class Meta:
        fields = ("authority",)
