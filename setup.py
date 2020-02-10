from setuptools import setup, find_packages

setup(
  name = 'reformer_pytorch',
  packages = find_packages(exclude=['examples']),
  version = '0.12.6',
  license='MIT',
  description = 'Reformer, the Efficient Transformer, Pytorch',
  author = 'Phil Wang',
  author_email = 'lucidrains@gmail.com',
  url = 'https://github.com/lucidrains/reformer-pytorch',
  keywords = ['transformers', 'attention', 'artificial intelligence'],
  install_requires=[
      'revtorch>=0.2.4',
      'torch',
  ],
  classifiers=[
      'Development Status :: 4 - Beta',
      'Intended Audience :: Developers',
      'Topic :: Scientific/Engineering :: Artificial Intelligence',
      'License :: OSI Approved :: MIT License',
      'Programming Language :: Python :: 3.6',
  ],
)