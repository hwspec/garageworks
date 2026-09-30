if [ ! -f chisel-axi-utils/src/main/scala/axi/Axi4Lite.scala ] ; then
	echo "Not found: chisel-axi-utils"
	exit 0
fi

git submodule deinit -f chisel-axi-utils
git rm -f chisel-axi-utils
rm -rf .git/modules/chisel-axi-utils

git submodule add https://github.com/hwspec/garageworks.git garageworks

git add .gitmodules garageworks

echo "Replaced chisel-axi-utils with garageworks"
echo "Please commit and push"




	
