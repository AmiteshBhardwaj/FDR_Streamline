class PrivateResponseMiddleware:
    def __init__(self,get_response):
        self.get_response=get_response
    def __call__(self,request):
        response=self.get_response(request)
        if getattr(request,'user',None) and request.user.is_authenticated or request.path=='/login/':
            response['Cache-Control']='private, no-store'
        response['Referrer-Policy']='same-origin'
        response['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        return response
