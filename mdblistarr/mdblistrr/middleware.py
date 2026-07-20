from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

class StaffRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        path = request.path_info
        public = (reverse('login'), '/healthz')
        if path.startswith(settings.STATIC_URL) or path in public:
            return self.get_response(request)
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                return JsonResponse({'detail':'Authentication required.'}, status=401)
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")
        if not (user.is_active and (user.is_staff or user.is_superuser)):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                return JsonResponse({'detail':'Staff access required.'}, status=403)
            return redirect('login')
        return self.get_response(request)
