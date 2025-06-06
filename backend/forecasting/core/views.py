import joblib, json
import pandas as pd
from django.core.paginator import Paginator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .serializers import PredictionInputSerializer
from django.contrib.auth import login
from django.views.generic import CreateView, ListView
from django.urls import reverse_lazy
from .forms import CustomRegisterForm
from .models import SearchHistory
from django.contrib.auth.views import LoginView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.csrf import csrf_exempt
import math
from .pdf_utils import generate_report_pdf
from django.http import HttpResponse, BadHeaderError
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from forecasting import settings


# Загрузка моделей один раз при старте приложения
# model = joblib.load('core/ml/model.pkl')
# encoder = joblib.load('core/ml/encoder.pkl')
# scaler = joblib.load('core/ml/scaler.pkl')


class PredictAPIView(APIView):
    def post(self, request):
        # 1. Проверка валидности данных
        serializer = PredictionInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            # 2. Подготовка данных
            input_data = serializer.validated_data
            df = pd.DataFrame([input_data])

            # 3. Расчет производных фичей
            df['level_to_levels'] = df['level'] / df['levels']
            df['area_to_rooms'] = df['area'] / df['rooms'].clip(1)
            df['room_size'] = df['area'] / df['rooms'].clip(1)
            df['is_last_floor'] = (df['level'] == df['levels']).astype(int)

            # 4. Преобразование данных
            encoded = encoder.transform(df)
            scaled = scaler.transform(encoded)

            # 5. Предсказание
            price = math.ceil(int(model.predict(scaled)[0] / 50000)) * 50000

            if request.user.is_authenticated:
                SearchHistory.objects.create(
                    user=request.user,
                    search_data=request.data,
                    result=price
                )

            return Response({'price': (math.ceil(int(price / 50000)) * 50000)}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get(self, request):
        if not request.user.is_authenticated:
            return Response([], status=status.HTTP_200_OK)

        history = SearchHistory.objects.filter(user=request.user).order_by('-created_at')
        data = [{
            'id': item.id,
            'params': {
                'rooms': item.search_data.get('rooms'),
                'area': item.search_data.get('area'),
                'kitchen_area': item.search_data.get('kitchen_area'),
                'level': item.search_data.get('level'),
                'levels': item.search_data.get('levels'),
                'building_type': item.search_data.get('building_type'),
                'object_type': item.search_data.get('object_type'),
                'region': item.search_data.get('region'),
                'geo_lat': item.search_data.get('geo_lat'),
                'geo_lon': item.search_data.get('geo_lon'),
                'address': item.search_data.get('address'),
            },
            'result': item.result,
            'date': item.created_at.strftime("%d.%m.%Y %H:%M")
        } for item in history]

        return Response(data, status=status.HTTP_200_OK)


class CustomView(LoginView):
    template_name = 'html/login.html'
    success_url = reverse_lazy('index')


class RegisterView(CreateView):
    form_class = CustomRegisterForm
    template_name = 'html/register.html'
    success_url = reverse_lazy('index')

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)  # Автоматический вход
        return response


class FullHistoryView(LoginRequiredMixin, ListView):
    model = SearchHistory
    template_name = 'html/history.html'
    context_object_name = 'history_items'
    paginate_by = 10

    def get_queryset(self):
        return SearchHistory.objects.filter(user=self.request.user).order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page = self.request.GET.get('page')
        paginator = Paginator(self.get_queryset(), self.paginate_by)
        history_items = paginator.get_page(page)
        context['history_items'] = history_items
        return context


@require_http_methods(["DELETE"])
def delete_history_item(request, item_id):
    try:
        item = SearchHistory.objects.get(id=item_id, user=request.user)
        item.delete()
        return JsonResponse({'status': 'success'})
    except SearchHistory.DoesNotExist:
        return JsonResponse({'error': 'Record not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
def toggle_favorite(request, item_id):
    if request.method == 'POST':
        try:
            # 1. Проверяем, что запись принадлежит текущему пользователю
            item = SearchHistory.objects.get(id=item_id, user=request.user)

            # 2. Получаем текущее состояние is_favorite из БД
            current_state = item.is_favorite

            # 3. Инвертируем состояние
            item.is_favorite = not current_state
            item.save()

            return JsonResponse(
                {
                    'success': True,
                    'is_favorite': item.is_favorite
                }
            )

        except SearchHistory.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Record not found'}, status=404)
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid method'}, status=400)


@require_http_methods(["POST"])
@csrf_exempt
def generate_report(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Требуется авторизация'}, status=403)

    try:
        item_ids = json.loads(request.body).get('items', [])
        items = SearchHistory.objects.filter(id__in=item_ids, user=request.user)

        if not items.exists():
            return JsonResponse({'error': 'Не найдены выбранные записи'}, status=404)

        # Генерация PDF
        pdf_buffer = generate_report_pdf(request.user, items)

        # Создаем HttpResponse с PDF
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="report_{request.user.id}.pdf"'
        return response

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
