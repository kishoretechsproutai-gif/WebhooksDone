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
import re
import requests
from decimal import Decimal
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.db import connection
from CoreApplication.views import get_user_from_token


@csrf_exempt
def gemini_chatbot(request):
    """
    Chatbot endpoint (Stable & Deterministic):
    1. Sends models info + user question to Gemini (temperature=0).
    2. Extracts and validates SQL strictly.
    3. Executes the SQL safely.
    4. Sends question + SQL results back to Gemini for a natural language summary.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST request required"}, status=400)

    try:
        # ---------------------- Step 1: Authenticate user ----------------------
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        data = json.loads(request.body)
        question = data.get("question")
        if not question:
            return JsonResponse({"error": "Question is required"}, status=400)

        # ---------------------- Step 2: Prompt for SQL generation ----------------------
        prompt = """
You are an expert SQL generator for Django ORM databases using MariaDB.
You must return only valid SQL JSON strictly in this format:
{"sql": "SELECT ..."}

❗ Never use placeholders or variables like <month_number>, <tenant_id>, or <year>.
Always use concrete numeric or string literals directly in SQL examples.
Output must be syntactically valid and executable directly.

Allowed tables and model structure:

1-CoreApplication_Product
id, name, company_id, created_at, updated_at

2-CoreApplication_ProductVariant
shopify_id, product_id, sku, price, created_at

3-CoreApplication_Order
shopify_id, customer_id, company_id, created_at

4-CoreApplication_OrderLineItem
variant_id, order_id, quantity, price

5-CoreApplication_Customer
shopify_id, name, email, company_id

6-CoreApplication_PromotionalData
product_id, discount_percent, start_date, end_date

7-Testingproject_skuforecastmetrics
sku, predicted_sales_30, sell_through_rate, month, company_id

8-Testingproject_skuforecasthistory
sku, actual_sales, month, company_id, live_inventory, predicted_sales_30, reason

9-CoreApplication_CompanyUser
id, company, email, password, shopify_access_token, shopify_store_url, webhook_secret

10-CoreApplication_Location
shopify_id, company_id, name, address, city, region, country, postal_code

11-CoreApplication_Collection
company_id, shopify_id, title, handle, updated_at, image_src, created_at

12-CoreApplication_CollectionItem
collection_id, product_id, created_at, image_src

13-CoreApplication_PurchaseOrder
purchase_order_id, supplier_name, sku_id, order_date, delivery_date, quantity_ordered, company_id

14-Testingproject_InventoryValuation
company_id, inventory_value, month, currency, created_at, updated_at


🧠 Important Rules:
- Allowed operations: SELECT only.
- Prohibited: DELETE, UPDATE, DROP, INSERT, ALTER, TRUNCATE.
- Always filter using `company_id = 2` where applicable.
- Use real dates like '2025-06-01' or `MONTH(created_at)=6 AND YEAR(created_at)=2025`.
- Use correct table joins:
  * ProductVariant.shopify_id = OrderLineItem.variant_id
  * Order.shopify_id = OrderLineItem.order_id
  * Product.id = ProductVariant.product_id
  * Order.customer_id = Customer.shopify_id

🧩 Correct SQL Examples for Reference:

Example 1:
User: "How many orders were placed in June 2025?"
{"sql": "SELECT COUNT(shopify_id) AS number_of_sales FROM CoreApplication_Order WHERE YEAR(created_at)=2025 AND MONTH(created_at)=6 AND company_id=2;"}

Example 2:
User: "Which SKU sold the most in June 2025?"
{"sql": "SELECT V.sku, SUM(L.quantity) AS total_sold FROM CoreApplication_OrderLineItem L JOIN CoreApplication_ProductVariant V ON V.shopify_id=L.variant_id JOIN CoreApplication_Order O ON O.shopify_id=L.order_id WHERE O.company_id=2 AND YEAR(O.created_at)=2025 AND MONTH(O.created_at)=6 GROUP BY V.sku ORDER BY total_sold DESC LIMIT 1;"}

Example 3:
User: "What was the average predicted sales for August 2025?"
{"sql": "SELECT AVG(predicted_sales_30) AS avg_predicted_sales FROM Testingproject_skuforecastmetrics WHERE month='2025-08-01' AND company_id=2;"}

Example 4:
User: "Show monthly sales trend for 2024"
{"sql": "SELECT YEAR(O.created_at) AS year, MONTH(O.created_at) AS month, SUM(L.quantity * L.price) AS total_sales FROM CoreApplication_OrderLineItem L JOIN CoreApplication_Order O ON O.shopify_id=L.order_id WHERE O.company_id=2 AND YEAR(O.created_at)=2024 GROUP BY YEAR(O.created_at), MONTH(O.created_at) ORDER BY YEAR(O.created_at), MONTH(O.created_at);"}

Example 5:
User: "List top 5 SKUs by total sales value in 2025"
{"sql": "SELECT V.sku, SUM(L.quantity * L.price) AS total_sales_value FROM CoreApplication_OrderLineItem L JOIN CoreApplication_ProductVariant V ON V.shopify_id=L.variant_id JOIN CoreApplication_Order O ON O.shopify_id=L.order_id WHERE O.company_id=2 AND YEAR(O.created_at)=2025 GROUP BY V.sku ORDER BY total_sales_value DESC LIMIT 5;"}
"""

        final_text = f"{prompt}\n\nUser question: {question}"

        payload = {
            "contents": [{"parts": [{"text": final_text}]}],
            "generationConfig": {"temperature": 0}
        }

        headers = {
            "x-goog-api-key": settings.GEMINI_API_KEY,
            "Content-Type": "application/json"
        }

        # ---------------------- Step 3: Ask Gemini for SQL ----------------------
        gemini_sql_resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers=headers,
            data=json.dumps(payload)
        ).json()

        raw_text = gemini_sql_resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            return JsonResponse({"error": "Gemini did not return valid JSON", "response": raw_text}, status=400)

        sql_data = json.loads(match.group())
        sql_query = sql_data.get("sql")

        if not sql_query or "SELECT" not in sql_query.upper():
            return JsonResponse({"error": "Invalid SQL generated", "response": sql_data}, status=400)

        # ---------------------- Step 4: Safety & Fixups ----------------------
        replacements = {
            "T2.id": "T2.shopify_id",
            "T3.id": "T3.shopify_id",
            "SKUForecastMetrics": "Testingproject_skuforecastmetrics",
            "SKUForecastHistory": "Testingproject_skuforecasthistory",
        }
        for wrong, correct in replacements.items():
            sql_query = sql_query.replace(wrong, correct)

        sql_query = sql_query.replace("<tenant_id>", "2")  # fallback safety

        if any(word in sql_query.upper() for word in ["DELETE", "DROP", "UPDATE", "INSERT", "ALTER", "TRUNCATE"]):
            return JsonResponse({"error": "Destructive SQL blocked", "sql": sql_query}, status=400)

        print("\n✅ Final SQL:\n", sql_query)

        # ---------------------- Step 5: Execute SQL ----------------------
        with connection.cursor() as cursor:
            cursor.execute(sql_query)
            columns = [col[0] for col in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # Convert Decimal to float
        def convert_decimal(obj):
            if isinstance(obj, list):
                return [convert_decimal(i) for i in obj]
            elif isinstance(obj, dict):
                return {k: convert_decimal(v) for k, v in obj.items()}
            elif isinstance(obj, Decimal):
                return float(obj)
            return obj

        results = convert_decimal(results)

        # ---------------------- Step 6: Generate Summary ----------------------
        summary_prompt = f"""
You are an analytics assistant.
Answer the user's question in one or two human-like sentences using only the SQL results.

Examples:
Q: "How many sales in October?"
Results: [{{"total_sales": 400}}]
A: "A total of 400 sales were recorded in October."

Q: "Which SKU sold the most in June?"
Results: [{{"sku": "TNX0100XTE"}}]
A: "The top-selling SKU in June was TNX0100XTE."

Now answer:
Question: {question}
Results: {json.dumps(results, indent=2)}
"""

        summary_payload = {
            "contents": [{"parts": [{"text": summary_prompt}]}],
            "generationConfig": {"temperature": 0.3}
        }

        gemini_summary_resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers=headers,
            data=json.dumps(summary_payload)
        ).json()

        final_answer = gemini_summary_resp["candidates"][0]["content"]["parts"][0]["text"].strip()

        # ---------------------- Step 7: Return SQL + Answer ----------------------
        return JsonResponse({
            "sql": sql_query,
            "answer": final_answer
        })

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
