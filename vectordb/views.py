import os
import json
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from sentence_transformers import SentenceTransformer
import chromadb
from CoreApplication.models import Order, OrderLineItem
from CoreApplication.views import get_user_from_token

# Initialize model once
model = SentenceTransformer('all-MiniLM-L6-v2')

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MAX_CONTEXT_CHARS = 3000  # Limit context size sent to Gemini
MAX_MATCHES = 20          # Reduce vector DB matches to avoid token overload

class GenericVectorSearchView(APIView):
    authentication_classes = []  # Add JWT auth if needed
    permission_classes = []      # Add permissions if needed

    def post(self, request):
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        query_text = request.data.get("query")
        if not query_text:
            return Response({"error": "Query text is required"}, status=status.HTTP_400_BAD_REQUEST)

        print(f"[DEBUG] Received query: {query_text}")

        try:
            # Step 1: Query vector DB
            matches = self.query_vector_db(user.id, query_text, n_results=MAX_MATCHES)
            print(f"[DEBUG] Vector DB returned {len(matches)} matches")

            if not matches:
                return Response({"answer": "No relevant data found", "matches": []}, status=status.HTTP_200_OK)

            # Step 2: Send matches + query to Gemini
            ai_answer = self.send_to_gemini(query_text, matches)
            print(f"[DEBUG] Gemini answer: {ai_answer}")

            return Response({
                "answer": ai_answer,
                "matches": matches
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print(f"[ERROR] {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @staticmethod
    def query_vector_db(user_id, query_text, n_results=10):
        """
        Query Chroma DB for both Orders and OrderLineItems.
        """
        folder_path = f"D:/TROOBA_PRODUCTION/chroma_db/tenant_{user_id}"
        client = chromadb.PersistentClient(path=folder_path)
        collection_name = f"tenant_{user_id}"
        vector_collection = client.get_or_create_collection(name=collection_name)

        # Embed the query text
        query_embedding = model.encode([query_text], convert_to_tensor=False)
        print(f"[DEBUG] Query embedding generated")

        # Query vector DB
        results = vector_collection.query(
            query_embeddings=query_embedding,
            n_results=n_results,
            include=["documents", "distances", "metadatas"]
        )

        matches = []

        for i, doc in enumerate(results['documents'][0]):
            metadata = results['metadatas'][0][i]
            order_data = None
            line_item_data = None

            # --- Lookup Order ---
            order_id = metadata.get("id") or metadata.get("order_id") or metadata.get("shopify_id")
            if order_id:
                try:
                    if isinstance(order_id, int):
                        order = Order.objects.get(id=order_id, company_id=user_id)
                    else:
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
                    print(f"[DEBUG] Order not found for ID/shopify_id={order_id}")

            # --- Lookup Line Item ---
            line_item_id = metadata.get("id") or metadata.get("line_item_id") or metadata.get("variant_id")
            if line_item_id:
                try:
                    if isinstance(line_item_id, int):
                        line_item = OrderLineItem.objects.get(id=line_item_id, company_id=user_id)
                    else:
                        line_item = OrderLineItem.objects.filter(variant_id=line_item_id, company_id=user_id).first()

                    if line_item:
                        line_item_data = {
                            "id": line_item.id,
                            "order_id": line_item.order_id,
                            "product_id": line_item.product_id,
                            "variant_id": line_item.variant_id,
                            "quantity": line_item.quantity,
                            "price": float(line_item.price),
                            "total": float(line_item.total)
                        }
                    else:
                        print(f"[DEBUG] LineItem not found for variant_id={line_item_id}")
                except OrderLineItem.DoesNotExist:
                    print(f"[DEBUG] LineItem not found for ID={line_item_id}")

            matches.append({
                "text": doc,
                "distance": results['distances'][0][i],
                "metadata": metadata,
                "order": order_data,
                "line_item": line_item_data
            })

        return matches

    @staticmethod
    def send_to_gemini(query_text, matches):
        """
        Send the query + matches to Gemini AI to get a final answer.
        Truncate context if too large.
        """
        try:
            # Build context text (truncate if too large)
            context_text = json.dumps(matches, default=str)
            if len(context_text) > MAX_CONTEXT_CHARS:
                context_text = context_text[:MAX_CONTEXT_CHARS] + " ...[truncated]"

            payload = {
                "contents": [
                    {
                        "parts": [
                            {
                                "text": f"Use the following database context to answer the question.\nContext: {context_text}\nQuestion: {query_text}"
                            }
                        ]
                    }
                ]
            }

            headers = {
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            }

            print("[DEBUG] Calling Gemini generate...")
            response = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
                headers=headers,
                json=payload
            )
            print(f"[DEBUG] Gemini generate status: {response.status_code}")

            response_json = response.json()
            ai_answer = response_json.get("candidates", [{}])[0].get("content", [{}])[0].get("text", "No answer")
            print("[DEBUG] Gemini generated answer successfully")
            return ai_answer

        except Exception as e:
            print(f"[ERROR] Gemini call failed: {str(e)}")
            return "Failed to get AI answer"


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


Example 6:
User: "How many customers are registered under company 2?"

{"sql": "SELECT COUNT(*) AS total_customers FROM CoreApplication_Customer WHERE company_id=2;"}


Example 7:
User: "Show all products created in 2025"

{"sql": "SELECT id, name, created_at FROM CoreApplication_Product WHERE YEAR(created_at)=2025 AND company_id=2;"}


Example 8:
User: "How many variants have price greater than 1000?"

{"sql": "SELECT COUNT(*) AS high_price_variants FROM CoreApplication_ProductVariant WHERE price>1000;"}


Example 9:
User: "Show total sales amount for 2025"

{"sql": "SELECT SUM(L.quantity * L.price) AS total_sales FROM CoreApplication_OrderLineItem L JOIN CoreApplication_Order O ON O.shopify_id=L.order_id WHERE O.company_id=2 AND YEAR(O.created_at)=2025;"}


Example 10:
User: "Which supplier supplied the highest quantity in 2025?"

{"sql": "SELECT supplier_id, SUM(total_quantity) AS total_supplied FROM CoreApplication_PurchaseOrder WHERE YEAR(created_at)=2025 AND company_id=2 GROUP BY supplier_id ORDER BY total_supplied DESC LIMIT 1;"}


Example 11:
User: "Show SKUs with forecast error greater than 10%"

{"sql": "SELECT sku, forecast_error_percentage FROM CoreApplication_SKUForecastMetrics WHERE forecast_error_percentage>10 AND company_id=2;"}


Example 12:
User: "How many collection items are there in the collection 'Aariya Bridal Set'?"

{"sql": "SELECT COUNT(CI.product_id) AS number_of_items FROM CoreApplication_CollectionItem CI JOIN CoreApplication_Collection C ON CI.collection_id = C.id WHERE C.title = 'Aariya Bridal Set' AND C.company_id = 2;"}


Example 13:
User: "List all products under the collection 'Bridal Collection'"

{"sql": "SELECT P.name FROM CoreApplication_CollectionItem CI JOIN CoreApplication_Collection C ON CI.collection_id = C.id JOIN CoreApplication_Product P ON CI.product_id = P.id WHERE C.title = 'Bridal Collection' AND C.company_id=2;"}


Example 14:
User: "Show total sales per location for company 2"

{"sql": "SELECT L.name AS location_name, SUM(OL.price * OL.quantity) AS total_sales FROM CoreApplication_OrderLineItem OL JOIN CoreApplication_Order O ON OL.order_id=O.id JOIN CoreApplication_Location L ON O.location_id=L.id WHERE O.company_id=2 GROUP BY L.name ORDER BY total_sales DESC;"}


Example 15:
User: "Show total inventory value for each month in 2025"

{"sql": "SELECT DATE_FORMAT(valuation_month, '%Y-%m') AS month, SUM(total_value) AS total_inventory_value FROM CoreApplication_InventoryValuation WHERE YEAR(valuation_month)=2025 AND company_id=2 GROUP BY month ORDER BY month;"}

User: "Which category items sold more units in August month 2025?"

{"sql": "SELECT C.title AS category_name, SUM(L.quantity) AS total_units_sold FROM CoreApplication_OrderLineItem L JOIN CoreApplication_Order O ON O.shopify_id = L.order_id JOIN CoreApplication_ProductVariant PV ON PV.shopify_id = L.variant_id JOIN CoreApplication_Product P ON P.id = PV.product_id JOIN CoreApplication_CollectionItem CI ON CI.product_id = P.id JOIN CoreApplication_Collection C ON C.id = CI.collection_id WHERE O.company_id = 2 AND YEAR(O.created_at) = 2025 AND MONTH(O.created_at) = 8 GROUP BY C.title ORDER BY total_units_sold DESC LIMIT 1;"}

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
