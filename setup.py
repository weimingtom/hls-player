from setuptools import setup, find_packages
setup(
    name = "HLSPlayer",
    version = "0.1",
    packages = find_packages(),
    entry_points = {
        'console_scripts': [ 'hls-player = HLS.player:main' ]
        },

    author = "Marc-Andre Lureau",
    author_email = "marcandre.lureau@gmail.com",
    description = "HTTP Live Streaming player",
    license = "GNU GPL",
    keywords = "video streaming live",
)
