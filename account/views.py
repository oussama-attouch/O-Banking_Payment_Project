from django.shortcuts import render, redirect
from account.models import KYC, Account
from account.forms import KYCForm
from django.contrib import messages
from django.contrib.auth.decorators import login_required

# Define a view function named 'account' to handle user account dashboard
def account(request):
    # Check if the user is logged in
    if request.user.is_authenticated:
        try:
            # Retrieve KYC (Know Your Customer) information associated with the user
            kyc = KYC.objects.get(user=request.user)
        except:
            # If no KYC information found, show a warning message and redirect to KYC registration
            messages.warning(request, "You need to submit your KYC")
            return redirect("account:kyc-reg")
        
        # If KYC information exists, retrieve the user's account details
        account = Account.objects.get(user=request.user)
    else:
        # If the user is not logged in, show a warning message and redirect to the sign-in page
        messages.warning(request, "You need to log in to access the dashboard")
        return redirect("userauths:sign-in")

    # Create a context dictionary with KYC and account details
    context = {
        "kyc": kyc,
        "account": account,
    }
    # Render the 'account.html' template with the provided context
    return render(request, "account/account.html", context)

# Define another view function named 'kyc_registration' for KYC form submission
@login_required
def kyc_registration(request):
    # Get the logged-in user's information
    user = request.user
    # Retrieve the user's account details
    account = Account.objects.get(user=user)

    try:
        # Try to retrieve the user's existing KYC information
        kyc = KYC.objects.get(user=user)
    except:
        # If no existing KYC information found, set 'kyc' to None
        kyc = None
    
    # Check if the form is being submitted via POST request
    if request.method == "POST":
        # Create a KYCForm instance with submitted data and the existing KYC instance (if any)
        form = KYCForm(request.POST, request.FILES, instance=kyc)
        if form.is_valid():
            # If the form data is valid, save it to the database
            new_form = form.save(commit=False)
            new_form.user = user
            new_form.account = account
            new_form.save()
            # Show a success message and redirect to the account dashboard
            messages.success(request, "KYC Form submitted successfully. It's now under review.")
            return redirect("account:account")
    else:
        # If not a POST request, create a KYCForm instance with existing KYC data (if any)
        form = KYCForm(instance=kyc)
    
    # Create a context dictionary with account details, KYC form, and existing KYC information
    context = {
        "account": account,
        "form": form,
        "kyc": kyc,
    }
    # Render the 'kyc-form.html' template with the provided context
    return render(request, "account/kyc-form.html", context)

def dashboard(request):
     # Check if the user is logged in
    if request.user.is_authenticated:
        try:
            # Try to retrieve KYC (Know Your Customer) information associated with the user
            kyc = KYC.objects.get(user=request.user)
        except:
            # If no KYC information found, show a warning message and redirect to KYC registration
            messages.warning(request, "You need to submit your KYC")
            return redirect("account:kyc-reg")
        
        # If KYC information exists, retrieve the user's account details
        account = Account.objects.get(user=request.user)
    else:
        # If the user is not logged in, show a warning message and redirect to the sign-in page
        messages.warning(request, "You need to log in to access the dashboard")
        return redirect("userauths:sign-in")

    # Create a context dictionary with KYC and account details
    context = {
        "kyc": kyc,
        "account": account,
    }
    return render(request,"account/dashboard.html",context)
