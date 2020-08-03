mongo_up:
	sed -i "s/ENV=.*/ENV=local/g" .env
	./mongo.sh

mongo_down:
	sed -i "s/ENV=.*/ENV=local/g" .env
	docker stop mongo
	docker container rm mongo

docker_up:
	sed -i "s/ENV=.*/ENV=docker/g" .env
	docker-compose up --build -d

docker_down:
	sed -i "s/ENV=.*/ENV=docker/g" .env
	docker-compose down

prod_setup:
	sed -i "s/ENV=.*/ENV=prod/g" .env
	./k8s/setup.sh

prod_deploy:
	sed -i "s/ENV=.*/ENV=prod/g" .env
	skaffold run -f ./k8s/skaffold.yaml

test:
	black ./app/common/ --check
	black ./app/data_collector/ --check
	black ./app/graph_generator/ --check
	black ./app/thetrains_app/ --check
	black ./tests/ --check
	flake8 --max-line-length=99 ./app/common/
	flake8 --max-line-length=99 ./app/data_collector/
	flake8 --max-line-length=99 ./app/graph_generator/
	flake8 --max-line-length=99 ./app/thetrains_app/
	flake8 --max-line-length=99 ./tests/
	pydocstyle ./app/common/
	pydocstyle ./app/data_collector/
	pydocstyle ./app/graph_generator/
	pydocstyle ./app/thetrains_app/
	pydocstyle ./tests/
	pytest

clean:
	docker system prune -a --volumes