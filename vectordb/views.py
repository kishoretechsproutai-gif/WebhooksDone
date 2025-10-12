from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from sentence_transformers import SentenceTransformer
import chromadb
from CoreApplication.models import Order
from CoreApplication.views import get_user_from_token

# Initialize model once (used for all queries)
model = SentenceTransformer('all-MiniLM-L6-v2')


class GenericVectorSearchView(APIView):
    authentication_classes = []  # Add JWT auth if needed
    permission_classes = []      # Add permissions if needed

    def post(self, request):
        # Get user from JWT token
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        query_text = request.data.get("query")
        if not query_text:
            return Response({"error": "Query text is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            matches = self.query_vector_db(user.id, query_text, n_results=50, numeric_search=True)
            return Response({"matches": matches}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @staticmethod
    def query_vector_db(user_id, query_text, n_results=10, numeric_search=True):
        """
        Generic function to query tenant-specific vector DB.
        """
        folder_path = f"D:/TROOBA_PRODUCTION/chroma_db/tenant_{user_id}"
        client = chromadb.PersistentClient(path=folder_path)
        collection_name = f"tenant_{user_id}"
        vector_collection = client.get_or_create_collection(name=collection_name)

        # Embed the query text
        query_embedding = model.encode([query_text], convert_to_tensor=False)

        # Numeric ID filter if query is numeric
        where_filter = {}
        if numeric_search and query_text.isdigit():
            query_int = int(query_text)
            where_filter = {
                "$or": [
                    {"id": query_int}, {"shopify_id": query_int}, {"customer_id": query_int},
                    {"order_id": query_int}, {"product_id": query_int}, {"variant_id": query_int},
                    {"collection_id": query_int}, {"user_id": query_int}
                ]
            }

        # Query vector DB
        results = vector_collection.query(
            query_embeddings=query_embedding,
            n_results=n_results,
            where=where_filter if where_filter else None,
            include=["documents", "distances", "metadatas"]
        )

        # Format results
        matches = []
        for i, doc in enumerate(results['documents'][0]):
            metadata = results['metadatas'][0][i]
            order_data = None

            # Lookup Order if order_id exists
            order_id = metadata.get("order_id")
            if order_id:
                try:
                    order = Order.objects.get(shopify_id=order_id, company_id=user_id)
                    order_data = {
                        "id": order.id,
                        "shopify_id": order.shopify_id,
                        "order_number": order.order_number,
                        "order_date": str(order.order_date),
                        "fulfillment_status": order.fulfillment_status,
                        "financial_status": order.financial_status,
                        "currency": order.currency,
                        "total_price": float(order.total_price),
                        "subtotal_price": float(order.subtotal_price),
                        "total_tax": float(order.total_tax),
                        "total_discount": float(order.total_discount),
                        "region": order.region,
                        "created_at": str(order.created_at),
                        "updated_at": str(order.updated_at)
                    }
                except Order.DoesNotExist:
                    order_data = None

            matches.append({
                "text": doc,
                "distance": results['distances'][0][i],
                "metadata": metadata,
                "order": order_data
            })

        return matches





import json
import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.db import connection
from CoreApplication.views import get_user_from_token

@csrf_exempt
def gemini_chatbot(request):
    """
    Chatbot endpoint: sends models info + user question to Gemini 2.5 Flash.
    Returns SQL execution results after Gemini generates strict JSON SQL.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST request required"}, status=400)

    try:
        # Authenticate user
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        # Get user question
        data = json.loads(request.body)
        question = data.get("question")
        if not question:
            return JsonResponse({"error": "Question is required"}, status=400)

        # ---------------------- Build prompt with models + examples ----------------------
        prompt = """
You are an expert AI assistant. Use the following models and examples to generate **safe SQL queries only in JSON format**.
Do NOT use DROP, DELETE, or any destructive operations.
Return **strict JSON** like: {"sql": "<your SQL query here>"}

CoreApplication Models (table prefix: CoreApplication_):

1. CompanyUser:
- id, company, email, password, shopify_access_token, shopify_store_url, webhook_secret
- Example: {id: 2, company: 'Tarinika', email: 'info@example.com'}

2. Customer:
- id, shopify_id, company_id, email, first_name, last_name, phone, created_at, updated_at, city, region, country, total_spent
- Example: {id: 1, shopify_id: 6688420298818, company_id: 2, email: 'abc@example.com', first_name: 'John', last_name: 'Doe', total_spent: 1234.56}

3. Location:
- id, shopify_id, company_id, name, address, city, region, country, postal_code
- Example: {id: 1, shopify_id: 123456, company_id: 2, name: 'Warehouse A', city: 'New York', region: 'NY'}

4. Product:
- id, shopify_id, company_id, title, vendor, product_type, tags, created_at, updated_at, status
- Example: {id: 1, shopify_id: 7537394876482, company_id: 2, title: '3KG0005XTC', vendor: 'Tarinika', product_type: 'Armlets', tags: 'Antique gold, Armlets'}

5. ProductVariant:
- id, shopify_id, product_id, company_id, title, sku, price, compare_at_price, cost, inventory_quantity, created_at, updated_at
- Example: {id: 1, shopify_id: 42419720192066, product_id: 7537394876482, company_id: 2, title: 'Default Title', sku: '3KG0005XTC', price: 59.99, inventory_quantity: 10}

6. Order:
- id, shopify_id, company_id, customer_id, order_number, order_date, fulfillment_status, financial_status, currency, total_price, subtotal_price, total_tax, total_discount, region
- Example: {id: 1, shopify_id: 6380303974466, company_id: 2, customer_id: 6688420298818, order_number: '61912', order_date: '2025-09-08', total_price: 199.18, region: 'United States'}

7. OrderLineItem:
- id, shopify_line_item_id, order_id, product_id, variant_id, quantity, price, discount_allocated, total, company_id
- Example: {id: 1, shopify_line_item_id: 15980659376194, order_id: 6380303974466, product_id: 7335578861634, variant_id: 41607093649474, quantity: 1, price: 19.99, total: 19.99}

8. PromotionalData:
- id, user_id, date, clicks, impressions, cost, conversions, conversion_value, ctr, avg_cpc, conv_value_per_cost, conversion_rate, cost_per_conversion, currency_code, image_url, price, title, variant_id
- Example: {id: 5, user_id: 6, date: '2025-09-11', clicks: 5, impressions: 351, cost: 1.34, conversions: 0, conversion_value: 0.00, price: 29.99, title: 'Tarinika|Armlets', variant_id: 14964261519426}

9. Collection:
- id, company_id, shopify_id, title, handle, updated_at, image_src, created_at
- Example: {id: 2, company_id: 2, shopify_id: 299823104066, title: '925 Silver', handle: '925-silver'}

10. CollectionItem:
- id, collection_id, product_id, created_at, image_src
- Example: {id: 1, collection_id: 1, product_id: 7526721060930, created_at: '2025-09-08'}

Testingproject Models (table prefix: Testingproject_):

11. SKUForecastMetrics:
- id, sku, category, month, forecast_accuracy, forecast_bias, days_of_inventory, sell_through_rate, inventory_turnover, created_at, company_id
- Example: {id: 1, sku: 'TEX0096XTE', category: 'Earrings', month: '2025-01-01', forecast_accuracy: 85.71, inventory_turnover: 7, company_id: 2}

12. SKUForecastHistory:
- id, sku, month, predicted_sales_30, actual_sales_30, reason, error, error_reason, predicted_sales_60, predicted_sales_90, live_inventory, company_id
- Example: {id: 1, sku: 'TEX0096XTE', month: '2025-01-01', predicted_sales_30: 6, actual_sales_30: 7, reason: 'Slight upward trend', predicted_sales_60: 8, predicted_sales_90: 10, live_inventory: 0, company_id: 2}

Use this information to generate **strict JSON SQL only** based on user's question.
        """

        # Combine prompt and user question
        final_text = f"{prompt}\n\nUser question: {question}"

        payload = {
            "contents": [
                {"parts": [{"text": final_text}]}
            ]
        }

        headers = {
            "x-goog-api-key": settings.GEMINI_API_KEY,
            "Content-Type": "application/json"
        }

        # ---------------------- Call Gemini API ----------------------
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers=headers,
            data=json.dumps(payload)
        )
        gemini_result = response.json()

        # ---------------------- Extract strict JSON SQL from Gemini response ----------------------
        try:
            sql_text = gemini_result['candidates'][0]['content']['parts'][0]['text']
            # Strip ```json ... ``` if present
            if sql_text.startswith("```"):
                sql_text = "\n".join(sql_text.split("\n")[1:-1])
            sql_json = json.loads(sql_text)
            sql_query = sql_json.get("sql")
        except Exception as e:
            return JsonResponse({
                "error": "Failed to parse Gemini SQL",
                "details": str(e),
                "gemini_response": gemini_result
            }, status=500)

        # ---------------------- Execute SQL ----------------------
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql_query)
                columns = [col[0] for col in cursor.description]
                results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            return JsonResponse({
                "error": "SQL execution failed",
                "details": str(e),
                "sql": sql_query
            }, status=500)

        # Return executed results along with Gemini response
        return JsonResponse({
            "question": question,
            "sql": sql_query,
            "results": results,
            "gemini_response": gemini_result
        })

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
