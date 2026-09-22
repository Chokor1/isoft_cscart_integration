from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in isoft_cscart_integration/__init__.py
from isoft_cscart_integration import __version__ as version

setup(
	name="isoft_cscart_integration",
	version=version,
	description="CS-Cart storefront integration for ERPNext (stock, price, product sync)",
	author="ITEC",
	author_email="ai.accounts@itec.co.ao",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
