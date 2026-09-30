from rest_framework.response import Response
from rest_framework.views import APIView

from .schema import health_check_schema

# Create your views here.

@health_check_schema
class HealthCheckView(APIView):

    authentication_classes = []
    permission_classes = []

    def get(self,request):
        return Response(
            {
                "status": "ok",
                "service": "auth-service"
            }
        )
