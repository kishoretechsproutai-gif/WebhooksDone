import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.models import Prompt, CompanyUser


@csrf_exempt
def get_all_prompts(request):
    """Get all prompts along with company user details"""
    if request.method != "GET":
        return JsonResponse({"error": "Only GET allowed"}, status=405)

    all_data = []
    prompts = Prompt.objects.all()

    for p in prompts:
        company_name = p.company
        company_users = CompanyUser.objects.filter(company=company_name).values(
            "id", "company", "email"
        )
        all_data.append({
            "id": p.id,
            "company": company_name,
            "prompt": p.prompt,
            "company_users": list(company_users)
        })

    return JsonResponse(all_data, safe=False)


@csrf_exempt
def get_prompt(request, prompt_id):
    """Get single prompt with company user details"""
    if request.method != "GET":
        return JsonResponse({"error": "Only GET allowed"}, status=405)

    try:
        p = Prompt.objects.get(id=prompt_id)
        company_users = CompanyUser.objects.filter(company=p.company).values(
            "id", "company", "email"
        )
        return JsonResponse({
            "id": p.id,
            "company": p.company,
            "prompt": p.prompt,
            "company_users": list(company_users)
        })
    except Prompt.DoesNotExist:
        return JsonResponse({"error": "Prompt not found"}, status=404)


@csrf_exempt
def create_prompt(request):
    """Create a new prompt"""
    if request.method != "POST":
        return JsonResponse({"error": "Only POST allowed"}, status=405)

    try:
        data = json.loads(request.body)
        company = data.get("company")
        prompt_text = data.get("prompt")

        if not company or not prompt_text:
            return JsonResponse({"error": "company and prompt are required"}, status=400)

        prompt = Prompt.objects.create(
            company=company,
            prompt=prompt_text,
        )
        return JsonResponse({"message": "Prompt created", "id": prompt.id}, status=201)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def update_prompt(request, prompt_id):
    """Update an existing prompt"""
    if request.method != "PUT":
        return JsonResponse({"error": "Only PUT allowed"}, status=405)

    try:
        p = Prompt.objects.get(id=prompt_id)
        data = json.loads(request.body)
        p.company = data.get("company", p.company)
        p.prompt = data.get("prompt", p.prompt)
        p.save()
        return JsonResponse({"message": "Prompt updated successfully"})
    except Prompt.DoesNotExist:
        return JsonResponse({"error": "Prompt not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def delete_prompt(request, prompt_id):
    """Delete a prompt"""
    if request.method != "DELETE":
        return JsonResponse({"error": "Only DELETE allowed"}, status=405)

    try:
        p = Prompt.objects.get(id=prompt_id)
        p.delete()
        return JsonResponse({"message": "Prompt deleted successfully"})
    except Prompt.DoesNotExist:
        return JsonResponse({"error": "Prompt not found"}, status=404)
