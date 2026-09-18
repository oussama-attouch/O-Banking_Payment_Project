from django.db import models
import uuid
from shortuuid.django_fields import ShortUUIDField
from userauths.models import User  # Importing the User model from another module
from django.db.models.signals import post_save  # Importing a signal for post-save actions

# Function to determine the directory path for user-uploaded files
def user_directory_path(instance, filename):
    ext = filename.split(".")[-1]
    filename = "%s_%s" % (instance.id, ext)  # Create a new filename
    return "user_{0}/{1}".format(instance.user.id, filename)  # Return the path

# Choices for the account status
ACCOUNT_STATUS_CHOICES = [
    ('active', 'Active'),
    ('inactive', 'Inactive'),
    ('in-review', 'In Review'),
]

# Choices for marital status
MARRTIAL_STATUS = {
    ("married", "Married"),
    ("single", "Single"),
    ("other", "Other")
}

# Choices for gender
GENDER = {
    ("male", "Male"),
    ("female", "Female"),  
    ("other", "Other")
}

# Choices for identity types
IDENTITY_TYPE = [
    ("passport", "Passport"),
    ("driver_license", "Driver's License"),
    ("national_id", "National ID"),
]


# Definition of the Account model
class Account(models.Model):
    # Primary key field using UUID, which is automatically generated and not editable
    Id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4, editable=False)
    
    # One-to-one relationship with the User model
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    
    # Decimal field for account balance
    account_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    
    # ShortUUIDField for account number, with specified parameters
    account_number = ShortUUIDField(unique=True, length=10, max_length=25, prefix="217", alphabet="1234567890")
    
    # ShortUUIDField for account ID, with specified parameters
    account_id = ShortUUIDField(unique=True, length=7, max_length=25, prefix="DEX", alphabet="1234567890")
    
    # ShortUUIDField for account PIN, with specified parameters
    account_pin = ShortUUIDField(unique=True, length=4, max_length=7, alphabet="1234567890")
    
    # ShortUUIDField for a reference code, with speci<fied parameters
    ref_code = ShortUUIDField(unique=True, length=10, max_length=10, alphabet="abcdfgh1234567890")
    
    # Char field for account status, with choices from ACCOUNT_STATUS_CHOICES
    account_status = models.CharField(max_length=100, choices=ACCOUNT_STATUS_CHOICES, default="inactive")
    
    # DateTime field for the creation date, set automatically on creation
    date = models.DateTimeField(auto_now_add=True)
    
    # Boolean field for KYC (Know Your Customer) submission status
    kyc_submitted = models.BooleanField(default=False)
    
    # Boolean field for KYC confirmation status
    kyc_confirmed = models.BooleanField(default=False)
    
    # ForeignKey relationship to the User model for the recommending user
    # recommended_by = models.ForeignKey(User, on_delete=models.DO_NOTHING, blank=True, null=True, related_name="recommended_by")
    
 # Meta class for the model, specifying ordering by date in descending order
    class Meta:
        ordering = ['-date']

 # Define a special method __str__ for the Account model
        def __str__(self):
        # Return a formatted string representation of the Account object
         return f"{self.user}"

# Define the KYC model
class KYC(models.Model):
    # Primary key field using UUID, automatically generated and not editable
    id = models.UUIDField(primary_key=True, unique=True, default=uuid.uuid4, editable=False)
    
    user = models.OneToOneField(User,on_delete=models.CASCADE)

    account = models.OneToOneField(Account,on_delete=models.CASCADE,null=True,blank=True)

    # Field for full name, with maximum length of 1000 characters
    full_name = models.CharField(max_length=1000)
    
    # Field for image upload, stored in the "Kyc" directory, with a default image
    image = models.ImageField(upload_to="kyc", default="default.jpg")
    
    # Field for nationality, with maximum length of 100 characters
    nationality = models.CharField(max_length=100)
    
    # Field for marital status, with choices from the MARTIAL_STATUS defined elsewhere
    marrital_status = models.CharField(choices=MARRTIAL_STATUS, max_length=40)
    
    # Field for gender, with choices from the GENDER defined elsewhere
    gender = models.CharField(choices=GENDER, max_length=40)
    
    # Field for identity type, with choices from the IDENTITY_TYPE (not defined here)
    identity_type = models.CharField(choices=IDENTITY_TYPE, max_length=140)
    
    # Field for date of birth, allowing user input
    date_of_birth = models.DateTimeField(auto_now_add=False)
    
    # Field for signature upload, stored in the "kyc" directory
    signature = models.ImageField(upload_to="kyc")
    
    # Address fields
    country = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    
    # Contact detail fields
    mobile = models.CharField(max_length=1000)
    fax = models.CharField(max_length=1000)
    
    # DateTime field for the creation date, set automatically on creation
    date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
    # Return a formatted string representation of the Account object
        return f"{self.user}"
    class Meta:
        ordering = ['-date']

    # Signal function to create an Account instance when a User instance is created
    def create_account(sender, instance, created, **kwargs):
        if created:
            Account.objects.create(user=instance)

    # Signal function to save the Account instance when a User instance is saved
    def save_account(sender, instance, **kwargs):
        instance.account.save()

    # Connect the create_account signal to the User model's post-save signal
    post_save.connect(create_account, sender=User)

    # Connect the save_account signal to the User model's post-save signal
    post_save.connect(save_account, sender=User)
