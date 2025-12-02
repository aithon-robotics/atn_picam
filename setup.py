from setuptools import setup, find_packages

setup(
    name="atn_picam",
    version="0.1.0",
    description="Video streaming and recording package for Jetson and Pi Zero",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Aithon Robotics",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.7",
    install_requires=[
        "flask",
        "flask-cors",
        "psutil",
        "aiohttp",
        "aiortc",
        "av",
    ],
    extras_require={
        "jetson": [],
        "pizero": [],
    },
    entry_points={
        "console_scripts": [
            "atn-jetson-stream=atn_picam.jetson.stream:main",
            "atn-pizero-stream=atn_picam.pizero.stream:main",
            "atn-pizero-webrtc=atn_picam.pizero.webrtc:main",
            "atn-jetson-webrtc=atn_picam.jetson.webrtc:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
    ],
)
